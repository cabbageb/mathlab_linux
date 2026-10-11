from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar
from datetime import timedelta, timezone
import asyncio
import hashlib
import json
import re
import shutil
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from math_lab_core.vnext.agent_backend import AgentTurn
from math_lab_core.vnext.artifacts import ArtifactStore, sha256_file
from math_lab_core.vnext.db import (
    AgentOperationCheckpointRecord,
    AgentThreadRecord,
    ArchitectureCommunicationRecord,
    ArchitectureDecisionLedgerRecord,
    ArchitectureReviewRecord,
    ArchitectureSnapshotRecord,
    ContractVersionRecord,
    Database,
    GlobalStateDeltaRecord,
    PromotionProposalRecord,
    ResearchFindingRecord,
    ResearchGraphEdgeRecord,
    ResearchNodeRecord,
    ResearchRunRecord,
    RunAttemptRecord,
    RuntimeEventRecord,
    VerifiedClaimRecord,
    WorkerAssignmentRecord,
    WorkerIdentityRecord,
    WorkerReportRecord,
    new_id,
    utc_iso,
    utc_now,
)
from math_lab_core.vnext.repositories import RepositoryHub
from math_lab_core.vnext.settings import VNextSettings
from math_lab_core.vnext.final_snapshots import FinalSnapshotService

from .latex_publication import (
    LatexCompilation,
    LatexPublicationCompiler,
    normalize_latex_output,
)
from .paper_corpus import build_paper_source_corpus
from .config import PublicationLiteratureConfig
from .literature_access import LiteratureLimits
from .public_summaries import (
    PublicSummarySource,
    fallback_public_summary,
    parse_public_summary,
    public_summary_task,
    summary_model_for_policy,
)
from .node_public_cards import (
    NodePublicCard,
    node_public_card_task,
    parse_node_public_card,
)
from .publication_literature import (
    filter_scout_report_to_frozen_evidence,
    scout_source_count,
    validate_scout_evidence,
    validate_scout_markdown,
)
from .vnext_contracts import (
    ArchitectureDecision,
    SupervisorDecision,
    ValidationResult,
    VerificationDecision,
    WorkerReport,
    markdown_section,
    parse_candidate_identity,
    parse_architecture_review,
    parse_supervisor,
    parse_verification,
    parse_worker_report,
    validate_supervisor_candidate,
)

Parsed = TypeVar("Parsed")
Parser = Callable[[str], tuple[Parsed | None, ValidationResult]]

PERSONA_TO_ROLE = {
    "literature": "literature_worker",
    "construction": "construction_worker",
    "general_proof": "general_proof_worker",
    "cross_domain": "cross_domain_worker",
}
TERMINAL_NODE_STATES = {"completed", "retired", "blocked", "failed"}
ACTIVE_RUN_STATES = {"created", "queued", "resuming", "running", "draining", "paused"}
VERIFICATION_POLICY_VERSION = "claim-v2"


class DynamicProverError(RuntimeError):
    pass


class RoleContractError(DynamicProverError):
    """表示角色已耗尽格式修复；保留最后一次原始交付供局部收束。"""

    def __init__(self, role_key: str, message: str, last_markdown: str = "") -> None:
        super().__init__(message)
        self.role_key = role_key
        self.last_markdown = last_markdown


class DynamicResearchController:
    """Bounded asynchronous Dynamic Research Graph controller.

    Two independent loops are maintained:
    * local node loop: persistent worker identities + assignment versions + streaming supervisor reviews;
    * global loop: ready-node scheduler + streaming architecture reviews.
    """

    def __init__(self, settings: VNextSettings, database: Database, artifacts: ArtifactStore, backend: Any) -> None:
        self.settings = settings
        self.database = database
        self.artifacts = artifacts
        self.backend = backend
        self.latex_compiler = LatexPublicationCompiler(
            timeout_seconds=settings.latex_compile_timeout_seconds,
        )
        self.final_snapshots = FinalSnapshotService(
            database, artifacts, settings.runtime_retention_days
        )
        self._run_semaphores: dict[str, asyncio.Semaphore] = {}
        self._accepting_runs = True
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._node_tasks: dict[str, dict[str, asyncio.Task[None]]] = {}
        self._worker_tasks: dict[str, dict[str, asyncio.Task[tuple[str, WorkerReport]]]] = {}
        self._architecture_tasks: dict[str, asyncio.Task[None]] = {}
        self._architecture_locks: dict[str, asyncio.Lock] = {}
        self._verification_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._architecture_poll_at: dict[str, float] = {}
        self._architecture_failure_until: dict[str, float] = {}
        self._run_stop_events: dict[str, asyncio.Event] = {}
        self._node_draining: dict[str, asyncio.Event] = {}
        self._completion_requests: dict[str, str] = {}
        self._shutdown_runs: set[str] = set()
        self._deletion_runs: set[str] = set()
        self._public_summary_tasks: dict[str, asyncio.Task[None]] = {}
        self._public_summary_runs: dict[str, set[str]] = {}
        self._node_public_card_tasks: dict[str, asyncio.Task[None]] = {}
        self._pause_locks: dict[str, asyncio.Lock] = {}
        self._pause_tasks: dict[str, asyncio.Task[None]] = {}
        self._capacity_locks: dict[str, asyncio.Lock] = {}
        self._capacity_incidents: dict[str, list[tuple[Any, str]]] = {}
        self._capacity_holds: dict[str, Any] = {}

    def _run_semaphore(self, run_id: str) -> asyncio.Semaphore:
        """为每个 Research Run 保留独立模型并发额度，恢复时复用同一实例。"""
        semaphore = self._run_semaphores.get(run_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.settings.internal_max_parallel_agent_calls)
            self._run_semaphores[run_id] = semaphore
        return semaphore

    async def start(self) -> None:
        # The web/API process only enqueues Research Runs.  It must never reconcile,
        # claim, execute, drain, or otherwise mutate runner-owned runtime state.
        if self.settings.process_role == "web":
            return
        if not self.settings.offline_agents:
            self.latex_compiler.preflight()
        await self.backend.start()
        self._reconcile_abandoned_runs()
        self._restore_public_summary_jobs()
        self._restore_node_public_card_jobs()

    async def stop(self) -> None:
        """Gracefully stop only the research work owned by this process.

        Unclaimed queued runs belong to the durable database queue and must survive a
        runner restart.  Active/claimed runs first enter the same durable pause barrier
        as the user-facing pause action; architecture, node and worker tasks are then
        cancelled only after their operation checkpoints are resumable.
        """
        if self.settings.process_role == "web":
            return
        self._accepting_runs = False
        pending_pauses = [task for task in self._pause_tasks.values() if not task.done()]
        if pending_pauses:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending_pauses, return_exceptions=True),
                    timeout=self.settings.graceful_shutdown_hard_seconds,
                )
            except TimeoutError:
                for task in pending_pauses:
                    task.cancel()
                await asyncio.gather(*pending_pauses, return_exceptions=True)
        owned_run_ids = set(self._run_tasks)
        shutdown_pause_ids: list[str] = []
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            claimed = list(session.scalars(select(ResearchRunRecord).where(
                ResearchRunRecord.claimed_by_runner == self.settings.runner_id,
                ResearchRunRecord.status.in_(["queued", "resuming", "running", "draining"]),
            )))
            owned_run_ids.update(run.run_id for run in claimed)
            for run_id in owned_run_ids:
                run = session.get(ResearchRunRecord, run_id)
                if run is None:
                    continue
                if run.status == "queued":
                    run.claimed_by_runner = None
                    run.claimed_at = None
                    continue
                if run.status == "resuming":
                    run.status = "paused"
                    run.paused_at = run.paused_at or utc_now()
                    run.pause_reason = "service_shutdown"
                    run.resume_requested_at = None
                    run.claimed_by_runner = None
                    run.claimed_at = None
                    if run.attempts:
                        run.attempts[-1].status = "paused"
                    for operation in run.agent_operations:
                        if operation.status in {"running", "capacity_wait"}:
                            operation.status = "paused"
                            operation.pause_epoch = run.pause_epoch
                            operation.updated_at = utc_now()
                    continue
                if run.status not in {"running", "draining"}:
                    continue
                if run.pause_requested_at is None:
                    run.pause_requested_at = utc_now()
                    run.pause_requested_by = "service_shutdown"
                    run.pause_reason = "service_shutdown"
                    run.pause_epoch += 1
                    repo.append_event(
                        "run",
                        run_id,
                        "run_pause_requested",
                        "MathLab 服务停止，自动保存当前研究位置",
                        {"pause_epoch": run.pause_epoch, "requested_by": "service_shutdown"},
                    )
                shutdown_pause_ids.append(run_id)

        if shutdown_pause_ids:
            results = await asyncio.gather(*[
                self._enter_run_pause(run_id, reason="service_shutdown")
                for run_id in shutdown_pause_ids
            ], return_exceptions=True)
            for run_id, result in zip(shutdown_pause_ids, results):
                if not isinstance(result, Exception):
                    continue
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    run = session.get(ResearchRunRecord, run_id)
                    if run is not None:
                        repo.append_event(
                            "run",
                            run_id,
                            "run_pause_failed",
                            "服务停止时模型进程未能完整响应；暂停屏障已保留供重启恢复",
                            {"error": str(result), "pause_epoch": run.pause_epoch},
                        )

        paused_tasks: list[asyncio.Task[None]] = []
        with self.database.session() as session:
            paused_ids = set(session.scalars(select(ResearchRunRecord.run_id).where(
                ResearchRunRecord.run_id.in_(owned_run_ids),
                ResearchRunRecord.status == "paused",
            ))) if owned_run_ids else set()
        for run_id in paused_ids:
            task = self._run_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                paused_tasks.append(task)
        if paused_tasks:
            await asyncio.gather(*paused_tasks, return_exceptions=True)
        run_tasks = [
            self._run_tasks[run_id]
            for run_id in owned_run_ids
            if run_id not in paused_ids
            and run_id in self._run_tasks
            and not self._run_tasks[run_id].done()
        ]
        if run_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*run_tasks, return_exceptions=True),
                    timeout=self.settings.graceful_shutdown_hard_seconds,
                )
            except TimeoutError:
                for task in run_tasks:
                    task.cancel()
                await asyncio.gather(*run_tasks, return_exceptions=True)

        remaining = [
            task
            for mapping in list(self._node_tasks.values()) + list(self._worker_tasks.values())
            for task in mapping.values()
            if not task.done()
        ]
        remaining.extend(task for rid, task in self._architecture_tasks.items() if rid in owned_run_ids and not task.done())
        remaining.extend(task for task in self._public_summary_tasks.values() if not task.done())
        remaining.extend(task for task in self._node_public_card_tasks.values() if not task.done())
        if remaining:
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)

        await self.backend.stop()
        self._run_semaphores.clear()

    # ------------------------------------------------------------------ public API
    def launch_run(self, user_id: str, run_id: str, attempt_id: str) -> dict[str, Any]:
        if not self._accepting_runs:
            raise DynamicProverError("MathLab 正在停止，暂不接受新 Run")
        if self.settings.process_role == "web":
            return {"run_id": run_id, "status": "queued"}
        existing = self._run_tasks.get(run_id)
        if existing is not None and not existing.done():
            return {"run_id": run_id, "status": "running"}
        task = asyncio.create_task(self._run(user_id, run_id, attempt_id), name=f"mathlab-run-{run_id}")
        self._run_tasks[run_id] = task
        self._node_tasks.setdefault(run_id, {})
        self._run_stop_events.setdefault(run_id, asyncio.Event())
        self._run_semaphore(run_id)
        task.add_done_callback(lambda _: self._run_tasks.pop(run_id, None))
        return {"run_id": run_id, "status": "starting"}

    async def wait_run(self, run_id: str) -> None:
        task = self._run_tasks.get(run_id)
        if task is not None:
            await task

    def _restore_public_summary_jobs(self) -> None:
        """Runner 启动时恢复已经持久化但尚未完成的短摘要任务。"""
        with self.database.session() as session:
            pending = RepositoryHub(session, self.artifacts).pending_public_summary_requests()
            keys = [item.scope_id for item in pending]
        for source_key in keys:
            self._start_public_summary_task(source_key)

    def _start_public_summary_task(self, source_key: str) -> None:
        """为一个持久请求启动单飞摘要任务；同步测试环境没有 event loop 时留待恢复。"""
        existing = self._public_summary_tasks.get(source_key)
        if existing is not None and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._process_public_summary(source_key),
                name=f"mathlab-public-summary-{source_key}",
            )
        except RuntimeError:
            return
        self._public_summary_tasks[source_key] = task
        task.add_done_callback(lambda _task, key=source_key: self._public_summary_tasks.pop(key, None))

    async def _process_public_summary(self, source_key: str) -> None:
        """用摘要专用模型处理一个持久请求，失败后只发布安全短句而不暴露原始长文。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            completed = repo.latest_public_summary(source_key)
            if completed is not None:
                return
            requested = next(
                (item for item in repo.read_events("public_summary", source_key) if item.kind == "requested"),
                None,
            )
            if requested is None:
                return
            payload = dict(requested.payload or {})
            run_id = str(payload.get("run_id") or "")
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            user_id = run.owner_user_id
            attempt = max(run.attempts, key=lambda item: item.attempt_number, default=None)
            attempt_id = attempt.attempt_id if attempt is not None else None
            model = summary_model_for_policy(dict(run.execution_policy or {}))
            research_language = str(getattr(run, "research_language", "zh") or "zh")
            source = PublicSummarySource(
                source_key=source_key,
                mode=str(payload.get("mode") or "event"),
                title=str(payload.get("title") or "研究进展"),
                content=requested.message,
                node_id=str(payload.get("node_id") or "") or None,
                source_role=str(payload.get("source_role") or "") or None,
                source_created_at=str(payload.get("source_created_at") or "") or None,
            )
            self._public_summary_runs.setdefault(run_id, set()).add(source_key)

        summary: str | None = None
        provider_thread_id: str | None = None
        error = ""
        original_task = public_summary_task(source)
        current = original_task
        operation_id = f"public-summary-{source_key}"
        repair = 0
        while repair < 2:
            try:
                async with self._run_semaphore(run_id):
                    turn = await asyncio.wait_for(
                        self.backend.run_role(
                            role="public_summarizer",
                            scope_id=f"public-summary-{source_key}",
                            task=current,
                            persistent=True,
                            provider_thread_id=provider_thread_id,
                            repair_number=repair,
                            operation_id=operation_id,
                            model_profile={"public_summarizer": model},
                            runtime_root=self._run_runtime_root(run_id),
                            output_language=research_language,
                        ),
                        timeout=self.settings.public_summary_timeout_seconds,
                    )
                provider_thread_id = turn.thread_id or provider_thread_id
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).record_usage(
                        user_id=user_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        node_id=source.node_id,
                        component="prover",
                        role=f"public_summarizer:{source.mode}",
                        model=turn.model,
                        usage=turn.usage,
                        provider_thread_id=turn.thread_id,
                        usage_semantics=turn.usage_semantics,
                        cost_estimator=lambda normalized: self.settings.estimate_turn_cost(
                            turn.model, normalized
                        ),
                    )
                if turn.status == "paused":
                    await self._wait_until_resumed(run_id)
                    current = (
                        "Continue the same interrupted public-summary task and submit only the requested short summary."
                        if provider_thread_id
                        else original_task
                    )
                    continue
                if turn.status != "completed":
                    error = str(turn.error or turn.status)
                    if self._is_explicit_capacity_error(error):
                        await self._handle_capacity_failure(
                            run_id, operation_id, f"public_summarizer:{source.mode}", error
                        )
                        continue
                    current = (
                        f"上一轮摘要调用未完成：{error}\n请直接重新提交要求的短摘要。"
                        if provider_thread_id
                        else original_task
                    )
                    repair += 1
                    continue
                parsed, validation = parse_public_summary(
                    turn.text,
                    source.mode,
                    allow_verified_overflow=(source.mode == "verified" and repair == 1),
                    expected_language=research_language,
                    allow_language_mismatch=(repair == 1),
                )
                if validation.ok and parsed is not None:
                    summary = parsed
                    break
                error = validation.repair_message()
                current = error
                repair += 1
            except Exception as exc:
                error = str(exc)
                current = (
                    f"上一轮摘要调用失败：{error}\n请直接重新提交要求的短摘要。"
                    if provider_thread_id
                    else original_task
                )
                repair += 1

        fallback = summary is None
        if summary is None:
            summary = fallback_public_summary(source, language=research_language)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.complete_public_summary(
                source_key,
                summary,
                model=model["model"],
                reasoning_effort=model["reasoning_effort"],
                fallback=fallback,
                error=error or None,
            )
            if source.mode == "verified":
                claim = session.get(VerifiedClaimRecord, source_key)
                if claim is not None:
                    claim.public_summary = summary
            if source.mode == "event" and source.source_role in {
                "node_supervisor", "architecture_reviewer"
            }:
                category = "local_review" if source.source_role == "node_supervisor" else "global_review"
                title = (
                    "Local research group stage judgment"
                    if research_language == "en" and category == "local_review"
                    else (
                        "Global research review"
                        if research_language == "en"
                        else (
                            "局部研究组形成阶段判断"
                            if category == "local_review"
                            else "全局研究复盘"
                        )
                    )
                )
                repo.append_public_run_event(
                    run_id,
                    category=category,
                    title=title,
                    summary=summary,
                    source_role=source.source_role,
                    node_id=source.node_id,
                    source_key=source_key,
                    source_created_at=source.source_created_at,
                    summarizer=model,
                )

    async def _wait_public_summaries(self, run_id: str) -> None:
        """Run 发布终态前有界等待已启动摘要，保证完成页不会长期停留在 pending。"""
        keys = list(self._public_summary_runs.get(run_id, set()))
        tasks = [self._public_summary_tasks[key] for key in keys if key in self._public_summary_tasks]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.settings.public_summary_timeout_seconds * 2,
            )
        except TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for key in keys:
                self._force_public_summary_fallback(key, "Run finalization summary timeout")

    def _force_public_summary_fallback(self, source_key: str, error: str) -> None:
        """终态等待超时时持久化安全短句，避免完成页长期停留在 pending。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            if repo.latest_public_summary(source_key) is not None:
                return
            requested = next(
                (item for item in repo.read_events("public_summary", source_key) if item.kind == "requested"),
                None,
            )
            if requested is None:
                return
            payload = dict(requested.payload or {})
            run_id = str(payload.get("run_id") or "")
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            model = summary_model_for_policy(dict(run.execution_policy or {}))
            research_language = str(getattr(run, "research_language", "zh") or "zh")
            source = PublicSummarySource(
                source_key=source_key,
                mode=str(payload.get("mode") or "event"),
                title=str(payload.get("title") or "研究进展"),
                content=requested.message,
                node_id=str(payload.get("node_id") or "") or None,
                source_role=str(payload.get("source_role") or "") or None,
                source_created_at=str(payload.get("source_created_at") or "") or None,
            )
            summary = fallback_public_summary(source, language=research_language)
            repo.complete_public_summary(
                source_key,
                summary,
                model=model["model"],
                reasoning_effort=model["reasoning_effort"],
                fallback=True,
                error=error,
            )
            if source.mode == "event" and source.source_role in {
                "node_supervisor", "architecture_reviewer"
            }:
                repo.append_public_run_event(
                    run_id,
                    category="local_review" if source.source_role == "node_supervisor" else "global_review",
                    title=source.title,
                    summary=summary,
                    source_role=source.source_role,
                    node_id=source.node_id,
                    source_key=source_key,
                    source_created_at=source.source_created_at,
                    summarizer=model,
                )

    def _restore_node_public_card_jobs(self) -> None:
        """只恢复新版 Controller 已明确登记为 pending 的 Node 卡片。"""
        with self.database.session() as session:
            nodes = list(session.scalars(select(ResearchNodeRecord)))
            pending = [
                node.node_id
                for node in nodes
                if dict((node.details or {}).get("public_card") or {}).get("status") == "pending"
            ]
        for node_id in pending:
            self._start_node_public_card_task(node_id)

    def _queue_node_public_card(self, node_id: str) -> None:
        """持久标记新 Node 的公开卡片任务，并在当前 event loop 中异步执行。"""
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return
            details = dict(node.details or {})
            if details.get("public_card"):
                return
            details["public_card"] = {"status": "pending", "node_version": node.node_version}
            node.details = details
        self._start_node_public_card_task(node_id)

    def _start_node_public_card_task(self, node_id: str) -> None:
        existing = self._node_public_card_tasks.get(node_id)
        if existing is not None and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._process_node_public_card(node_id),
                name=f"mathlab-node-public-card-{node_id}",
            )
        except RuntimeError:
            return
        self._node_public_card_tasks[node_id] = task
        task.add_done_callback(
            lambda _task, key=node_id: self._node_public_card_tasks.pop(key, None)
        )

    async def _process_node_public_card(self, node_id: str) -> None:
        """用独立轻量角色生成 Node 展示卡片；失败不阻塞数学研究。"""
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return
            run = session.get(ResearchRunRecord, node.run_id)
            if run is None:
                return
            current_card = dict((node.details or {}).get("public_card") or {})
            if current_card.get("status") == "completed":
                return
            run_id = run.run_id
            user_id = run.owner_user_id
            attempt = max(run.attempts, key=lambda item: item.attempt_number, default=None)
            attempt_id = attempt.attempt_id if attempt else None
            model = summary_model_for_policy(dict(run.execution_policy or {}))
            research_language = str(getattr(run, "research_language", "zh") or "zh")
            source_kind, source_title, source_objective = node.kind, node.title, node.objective

        parsed: NodePublicCard | None = None
        provider_thread_id: str | None = None
        error = ""
        original_task = node_public_card_task(
            kind=source_kind,
            title=source_title,
            objective=source_objective,
        )
        current = original_task
        operation_id = f"node-public-card-{node_id}"
        repair = 0
        while repair < 2:
            try:
                async with self._run_semaphore(run_id):
                    turn = await asyncio.wait_for(
                        self.backend.run_role(
                            role="node_public_summarizer",
                            scope_id=f"node-public-card-{node_id}",
                            task=current,
                            persistent=True,
                            provider_thread_id=provider_thread_id,
                            repair_number=repair,
                            operation_id=operation_id,
                            model_profile={"public_summarizer": model},
                            runtime_root=self._run_runtime_root(run_id),
                            output_language=research_language,
                        ),
                        timeout=self.settings.public_summary_timeout_seconds,
                    )
                provider_thread_id = turn.thread_id or provider_thread_id
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).record_usage(
                        user_id=user_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        node_id=node_id,
                        component="prover",
                        role="node_public_summarizer",
                        model=turn.model,
                        usage=turn.usage,
                        provider_thread_id=turn.thread_id,
                        usage_semantics=turn.usage_semantics,
                        cost_estimator=lambda normalized: self.settings.estimate_turn_cost(
                            turn.model, normalized
                        ),
                    )
                if turn.status == "paused":
                    await self._wait_until_resumed(run_id)
                    current = (
                        "Continue the same interrupted node-card task and submit the complete card."
                        if provider_thread_id
                        else original_task
                    )
                    continue
                if turn.status != "completed":
                    error = str(turn.error or turn.status)
                    if self._is_explicit_capacity_error(error):
                        await self._handle_capacity_failure(
                            run_id, operation_id, "node_public_summarizer", error
                        )
                        continue
                    current = (
                        f"上一轮调用未完成：{error}\n请重新提交完整公开卡片。"
                        if provider_thread_id
                        else original_task
                    )
                    repair += 1
                    continue
                candidate, validation = parse_node_public_card(
                    turn.text,
                    expected_language=research_language,
                    allow_language_mismatch=(repair == 1),
                )
                if validation.ok and candidate is not None:
                    parsed = candidate
                    error = ""
                    break
                error = validation.repair_message()
                current = error
                repair += 1
            except Exception as exc:
                error = str(exc)
                current = (
                    f"上一轮调用失败：{error}\n请重新提交完整公开卡片。"
                    if provider_thread_id
                    else original_task
                )
                repair += 1

        if parsed is None:
            complete_objective = source_objective.strip()
            parsed = NodePublicCard(
                title=(
                    f"{source_kind.title()} research node"
                    if research_language == "en"
                    else source_title
                ),
                summary=(
                    "This research node is active; its detailed public summary is temporarily unavailable."
                    if research_language == "en"
                    else complete_objective
                ),
                raw_markdown=(
                    (
                        "# Research Node Overview\n\n## Title\n\n"
                        f"{source_title}\n\n## Current Research Question\n\n{complete_objective}\n"
                    )
                    if research_language == "en"
                    else (
                        "# Research Node 展示\n\n## 标题\n\n"
                        f"{source_title}\n\n## 当前研究问题\n\n{complete_objective}\n"
                    )
                ),
            )
        path = self._run_root(run_id) / "public" / "node-cards" / f"{node_id}.md"
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), parsed.raw_markdown)
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return
            details = dict(node.details or {})
            details["public_card"] = {
                "status": "completed",
                "title": parsed.title,
                "summary": parsed.summary,
                "path": self.artifacts.reference(path),
                "sha256": sha256_file(path),
                "model": model["model"],
                "reasoning_effort": model["reasoning_effort"],
                "fallback": bool(error),
                "error": error or None,
            }
            node.details = details

    async def terminate_run_for_deletion(self, user_id: str, run_id: str, *, timeout: float) -> None:
        """为永久删除终止 Run：取消子任务、结算已用额度，并明确跳过最终 Writer。"""
        with self.database.session() as session:
            run = RepositoryHub(session, self.artifacts).get_run(user_id, run_id)
            if run.status not in {"created", "queued", "resuming", "running", "draining", "paused"}:
                return
        deadline = time.monotonic() + max(0.1, timeout)
        self._deletion_runs.add(run_id)
        self._run_stop_events.setdefault(run_id, asyncio.Event()).set()
        tasks_to_cancel: set[asyncio.Task[Any]] = set()
        for node_id, node_task in list(self._node_tasks.get(run_id, {}).items()):
            self._node_draining.setdefault(node_id, asyncio.Event()).set()
            if not node_task.done():
                tasks_to_cancel.add(node_task)
            tasks_to_cancel.update(
                task
                for task in self._worker_tasks.get(node_id, {}).values()
                if not task.done()
            )
        architecture = self._architecture_tasks.get(run_id)
        if architecture is not None and not architecture.done():
            tasks_to_cancel.add(architecture)
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            tasks_to_cancel.add(task)
        for current in tasks_to_cancel:
            current.cancel()
        if tasks_to_cancel:
            _, pending = await asyncio.wait(
                tasks_to_cancel,
                timeout=max(0.1, deadline - time.monotonic()),
            )
            if pending:
                raise DynamicProverError(
                    "Research Run 及其子任务未能在删除截止时间内停止"
                )
        pause_backend = getattr(self.backend, "pause_run", None)
        if callable(pause_backend):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DynamicProverError("Research Run 删除清理已超过总截止时间")
            try:
                await asyncio.wait_for(
                    pause_backend(self._run_runtime_root(run_id), "deletion"),
                    timeout=min(10.0, remaining),
                )
            except TimeoutError as exc:
                raise DynamicProverError(
                    "Research Run transport 清理超过 10 秒；目标未删除，可重试"
                ) from exc
        self._mark_run_deleted_interrupted(user_id, run_id)

    def retry_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            attempt = RepositoryHub(session, self.artifacts).create_retry_attempt(user_id, run_id)
        return {"run_id": run_id, "attempt_id": attempt.attempt_id, "status": "queued"}

    def request_graceful_drain(self, user_id: str, run_id: str) -> dict[str, Any]:
        """持久请求结束研究阶段；Runner 收到后收束节点并正常进入 Writer。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.graceful_drain_requested_at is not None:
                return {"run_id": run_id, "status": run.status, "requested": True}
            if run.pause_requested_at is not None:
                raise DynamicProverError("Run 正在暂停；请恢复后再选择直接收束")
            if run.status != "running":
                raise DynamicProverError("只有正在研究的 Run 可以直接收束")
            now = utc_now()
            run.graceful_drain_requested_at = now
            run.graceful_drain_requested_by = user_id
            run.status = "draining"
            repo.append_event(
                "run",
                run_id,
                "user_graceful_drain_requested",
                "用户要求直接收束当前研究并进入论文写作",
                {"requested_by": user_id},
            )
        self._completion_requests[run_id] = "partially_resolved"
        self._run_stop_events.setdefault(run_id, asyncio.Event()).set()
        return {"run_id": run_id, "status": "draining", "requested": True}

    async def request_pause(self, user_id: str, run_id: str) -> dict[str, Any]:
        """先持久发布 paused 屏障，再由单飞后台任务停止模型进程。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status == "paused":
                return {
                    "run_id": run_id,
                    "status": run.status,
                    "pause_pending": False,
                }
            if run.pause_requested_at is None:
                if run.status not in {"running", "draining"}:
                    raise DynamicProverError("只有正在研究、收束或写作的 Run 可以暂停")
                run.pause_requested_at = utc_now()
                run.pause_requested_by = user_id
                run.pause_reason = "user"
                run.pause_epoch += 1
                repo.append_event(
                    "run", run_id, "run_pause_requested", "用户请求暂停全部研究调用",
                    {"pause_epoch": run.pause_epoch, "requested_by": user_id},
                )
            pause_reason = run.pause_reason or "user"
        self._persist_pause_barrier(run_id, reason=pause_reason)
        self._start_pause_task(run_id, reason=pause_reason)
        await asyncio.sleep(0)
        return {"run_id": run_id, "status": "paused", "pause_pending": False}

    def _start_pause_task(self, run_id: str, *, reason: str) -> asyncio.Task[None]:
        """为一个 Run 启动唯一后台暂停任务，并持久记录异常。"""
        existing = self._pause_tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._enter_run_pause(run_id, reason=reason),
            name=f"mathlab-pause-{run_id}",
        )
        self._pause_tasks[run_id] = task

        def settle(completed: asyncio.Task[None]) -> None:
            self._pause_tasks.pop(run_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is None:
                return
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                run = session.get(ResearchRunRecord, run_id)
                if run is None:
                    return
                repo.append_event(
                    "run",
                    run_id,
                    "run_pause_failed",
                    "暂停模型进程时发生错误；Run 保持暂停屏障，不会进入论文写作",
                    {"error": str(error), "pause_epoch": run.pause_epoch},
                )

        task.add_done_callback(settle)
        return task

    def request_resume(self, user_id: str, run_id: str) -> dict[str, Any]:
        """把 paused Run 放回原 Attempt；Runner 按并发额度恢复其 operation checkpoints。"""
        pause_task = self._pause_tasks.get(run_id)
        if pause_task is not None and not pause_task.done():
            raise DynamicProverError("研究正在完成暂停收口，请稍后再继续")
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status == "resuming":
                return {"run_id": run_id, "status": "resuming"}
            if run.status != "paused":
                raise DynamicProverError("只有已暂停的 Run 可以继续运行")
            now = utc_now()
            if run.paused_at is not None:
                paused_at = run.paused_at
                if paused_at.tzinfo is None:
                    paused_at = paused_at.replace(tzinfo=timezone.utc)
                run.total_paused_seconds += max(0.0, (now - paused_at).total_seconds())
            run.status = "resuming"
            run.resume_requested_at = now
            run.pause_requested_at = None
            run.pause_requested_by = None
            run.paused_at = None
            run.claimed_by_runner = run.claimed_by_runner if self._run_tasks.get(run_id) else None
            run.claimed_at = run.claimed_at if self._run_tasks.get(run_id) else None
            if run.attempts:
                run.attempts[-1].status = "resuming"
                run.attempts[-1].error = None
                run.attempts[-1].completed_at = None
            repo.append_event(
                "run", run_id, "run_resume_requested", "用户请求从暂停点继续研究",
                {"pause_epoch": run.pause_epoch},
            )
        return {"run_id": run_id, "status": "resuming"}

    async def _enter_run_pause(self, run_id: str, *, reason: str) -> None:
        """先确保 durable paused 屏障存在，再在后台停止目标 Run 的模型进程。"""
        lock = self._pause_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            self._persist_pause_barrier(run_id, reason=reason)
            pause_backend = getattr(self.backend, "pause_run", None)
            if callable(pause_backend):
                await pause_backend(self._run_runtime_root(run_id), reason)
            await asyncio.sleep(0.05)

    def _persist_pause_barrier(self, run_id: str, *, reason: str) -> None:
        """用单次事务立即冻结 Run 与当前 operation，不等待 transport 回收。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            if run.status not in {"running", "draining", "paused"}:
                return
            active_operations = list(session.scalars(select(AgentOperationCheckpointRecord).where(
                AgentOperationCheckpointRecord.run_id == run_id,
                AgentOperationCheckpointRecord.status.in_(["running", "capacity_wait"]),
            )))
            for operation in active_operations:
                operation.status = "paused"
                operation.pause_epoch = run.pause_epoch
                operation.updated_at = utc_now()
            if run.status == "paused":
                return
            pre_status = run.status
            pre_phase = self._observed_runtime_phase(session, run)
            paused_operation_ids = [item.operation_id for item in active_operations]
            paused_node_ids = sorted({
                str(item.node_id) for item in active_operations if item.node_id
            })
            run.pre_pause_status = pre_status
            run.pre_pause_phase = pre_phase
            run.status = "paused"
            run.paused_at = utc_now()
            run.pause_reason = reason
            run.last_heartbeat_at = utc_now()
            if run.attempts:
                run.attempts[-1].status = "paused"
            repo.append_event(
                "run", run_id, "run_paused",
                "模型接口容量受限，研究已安全暂停，可稍后继续" if reason == "provider_capacity" else "研究已安全暂停，可稍后继续",
                {
                    "pause_epoch": run.pause_epoch,
                    "reason": reason,
                    "pre_pause_phase": pre_phase,
                    "paused_node_ids": paused_node_ids,
                    "paused_operation_ids": paused_operation_ids,
                },
            )
            repo.append_event(
                "public_run", run_id, "run_paused",
                "模型接口容量受限，研究已安全暂停，可稍后继续。" if reason == "provider_capacity" else "研究已暂停；当前数学产物和运行位置均已保存。",
                {"title": "研究已暂停", "tone": "paused", "source_role": "controller"},
            )

    async def _wait_until_resumed(self, run_id: str) -> None:
        """等待 durable resume 请求，并仅由一个协程恢复 scoped transport 与 Run 状态。"""
        while True:
            with self.database.session() as session:
                run = session.get(ResearchRunRecord, run_id)
                if run is None:
                    raise DynamicProverError("暂停中的 Run 已不存在")
                status = run.status
            if status == "resuming":
                lock = self._pause_locks.setdefault(run_id, asyncio.Lock())
                async with lock:
                    with self.database.session() as session:
                        run = session.get(ResearchRunRecord, run_id)
                        if run is None:
                            raise DynamicProverError("暂停中的 Run 已不存在")
                        if run.status != "resuming":
                            continue
                        target_status = run.pre_pause_status or "running"
                    resume_backend = getattr(self.backend, "resume_run", None)
                    if callable(resume_backend):
                        await resume_backend(self._run_runtime_root(run_id))
                    with self.database.session() as session:
                        repo = RepositoryHub(session, self.artifacts)
                        run = session.get(ResearchRunRecord, run_id)
                        if run is None:
                            raise DynamicProverError("暂停中的 Run 已不存在")
                        if run.status == "resuming":
                            run.status = target_status
                            run.resume_requested_at = None
                            run.pause_reason = None
                            run.last_heartbeat_at = utc_now()
                            if run.attempts:
                                run.attempts[-1].status = "running"
                            repo.append_event(
                                "run", run_id, "run_resumed", "Research Run 已从暂停点继续",
                                {"pause_epoch": run.pause_epoch, "restored_status": target_status},
                            )
                            repo.append_event(
                                "public_run", run_id, "run_resumed", "研究已从保存的位置继续运行。",
                                {"title": "研究继续运行", "tone": "research", "source_role": "controller"},
                            )
                return
            if status in {"running", "draining"}:
                return
            if status in {"completed", "failed", "interrupted"}:
                raise DynamicProverError(f"暂停等待期间 Run 已进入终态：{status}")
            await asyncio.sleep(0.2)

    @staticmethod
    def _observed_runtime_phase(session: Any, run: ResearchRunRecord) -> str:
        """从最新 Attempt 的阶段事件恢复暂停前业务阶段。"""
        events = list(session.scalars(
            select(RuntimeEventRecord)
            .where(RuntimeEventRecord.scope_type == "run", RuntimeEventRecord.scope_id == run.run_id)
            .order_by(RuntimeEventRecord.sequence)
        ))
        last_reopened = max(
            (item.sequence for item in events if item.kind == "research_reopened"),
            default=0,
        )
        events = [item for item in events if item.sequence > last_reopened]
        kinds = {item.kind for item in events}
        if "paper_revision_started" in kinds:
            return "paper_revision"
        if "paper_audit_started" in kinds:
            return "paper_audit"
        if "paper_writing_started" in kinds or "run_final_state_reconciled" in kinds:
            return "paper_writing"
        if "closure_drain_completed" in kinds:
            return "draining"
        return "draining" if run.status == "draining" else "research"

    def run_payload(self, user_id: str, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            contract = session.get(ContractVersionRecord, run.contract_version_id)
            if contract is None or contract.conversation_id != run.conversation_id:
                raise DynamicProverError("Research Run 的锁定 Contract Version 不存在")
            nodes = repo.list_nodes(run_id)
            workers: list[WorkerIdentityRecord] = []
            assignments: list[WorkerAssignmentRecord] = []
            for node in nodes:
                workers.extend(repo.list_workers(node.node_id))
                assignments.extend(repo.list_assignments(node.node_id))
            execution_policy = dict(run.execution_policy or {})
            budget_seconds = int(execution_policy.get("duration_seconds") or 0)
            latest_attempt = max(run.attempts, key=lambda item: item.attempt_number, default=None)

            def aware_utc(value: Any) -> Any:
                if value is not None and value.tzinfo is None:
                    return value.replace(tzinfo=timezone.utc)
                return value

            terminal = run.status in {"completed", "failed", "interrupted"}
            stopped_at = (
                run.terminal_at
                or run.completed_at
                or (latest_attempt.completed_at if latest_attempt is not None else None)
            ) if terminal else None
            end_time = aware_utc(stopped_at) or aware_utc(run.paused_at) or utc_now()
            start_time = run.started_at
            if start_time is not None:
                start_time = aware_utc(start_time)
                end_time = aware_utc(end_time)
                elapsed_seconds = max(
                    0,
                    int((end_time - start_time).total_seconds() - float(run.total_paused_seconds or 0)),
                )
            else:
                elapsed_seconds = 0
            run_events = repo.read_events("run", run_id)
            attempt_started = aware_utc(latest_attempt.started_at) if latest_attempt else None
            phase_events = [
                item
                for item in run_events
                if attempt_started is None or aware_utc(item.created_at) >= attempt_started
            ]
            last_reopened = max(
                (item.sequence for item in phase_events if item.kind == "research_reopened"),
                default=0,
            )
            if last_reopened:
                phase_events = [item for item in phase_events if item.sequence > last_reopened]
            event_kinds = {item.kind for item in phase_events}
            if "paper_published" in event_kinds:
                observed_paper_phase = "paper_published"
            elif "paper_revision_started" in event_kinds:
                observed_paper_phase = "paper_revision"
            elif "paper_audit_started" in event_kinds:
                observed_paper_phase = "paper_audit"
            elif "paper_writing_started" in event_kinds or "run_final_state_reconciled" in event_kinds:
                observed_paper_phase = "paper_writing"
            else:
                observed_paper_phase = None
            if run.status == "completed":
                runtime_phase = "completed"
            elif run.status in {"failed", "interrupted"}:
                runtime_phase = run.status
            elif run.status in {"paused", "resuming"}:
                runtime_phase = "paused" if run.status == "paused" else "resuming"
            elif observed_paper_phase is not None:
                runtime_phase = observed_paper_phase
            elif "closure_drain_completed" in event_kinds:
                runtime_phase = "draining"
            elif run.status == "draining":
                runtime_phase = "draining"
            elif run.status in {"created", "queued"}:
                runtime_phase = "waiting"
            else:
                runtime_phase = "research"
            progress_phase = (
                (run.pre_pause_phase or observed_paper_phase or "research")
                if run.status in {"paused", "resuming"}
                else (
                    observed_paper_phase
                    if run.status in {"failed", "interrupted"} and observed_paper_phase is not None
                    else runtime_phase
                )
            )
            phase_event_kinds = {
                "paper_writing": "paper_writing_started",
                "paper_audit": "paper_audit_started",
                "paper_revision": "paper_revision_started",
                "paper_published": "paper_published",
            }
            phase_kind = phase_event_kinds.get(progress_phase)
            phase_event = next(
                (item for item in reversed(phase_events) if item.kind == phase_kind),
                None,
            ) if phase_kind else None
            paper_start = next(
                (item for item in reversed(phase_events) if item.kind == "paper_writing_started"),
                None,
            )
            phase_elapsed_seconds = 0
            if phase_event is not None:
                phase_elapsed_seconds = max(
                    0,
                    int((end_time - aware_utc(phase_event.created_at)).total_seconds()
                        - self._pause_overlap_seconds(phase_events, aware_utc(phase_event.created_at), end_time)),
                )
            publication_elapsed_seconds = 0
            if paper_start is not None:
                publication_elapsed_seconds = max(
                    0,
                    int((end_time - aware_utc(paper_start.created_at)).total_seconds()
                        - self._pause_overlap_seconds(phase_events, aware_utc(paper_start.created_at), end_time)),
                )
            verified_claims: list[dict[str, Any]] = []
            pause_task = self._pause_tasks.get(run_id)
            pause_transport_pending = bool(
                pause_task is not None and not pause_task.done()
            )
            for item in repo.list_verified_claims(run_id):
                summary_event = repo.latest_public_summary(item.verified_claim_id)
                summary_events = repo.read_events("public_summary", item.verified_claim_id)
                verified_claims.append(
                    {
                        "verified_claim_id": item.verified_claim_id,
                        "node_id": item.node_id,
                        "verdict": item.verdict,
                        "statement": item.statement,
                        "candidate_scope": item.candidate_scope,
                        "reuse_boundary": item.reuse_boundary,
                        "verification_policy_version": item.verification_policy_version,
                        "proof_available": bool(item.candidate_path),
                        "public_summary": item.public_summary or (summary_event.message if summary_event else None),
                        "summary_status": (
                            "completed"
                            if item.public_summary or summary_event is not None
                            else ("pending" if any(event.kind == "requested" for event in summary_events) else "missing")
                        ),
                    }
                )
            return {
                "run_id": run.run_id,
                "conversation_id": run.conversation_id,
                "contract_version_id": run.contract_version_id,
                "contract_version": contract.version,
                "status": run.status,
                "completion_kind": run.completion_kind,
                "graph_version": run.graph_version,
                "expected_cost": run.expected_cost,
                "reserved_credit": run.reserved_credit,
                "actual_cost": run.actual_cost,
                "execution_policy": execution_policy,
                "research_language": str(
                    getattr(run, "research_language", "zh") or "zh"
                ),
                "runtime_phase": runtime_phase,
                "progress_phase": progress_phase,
                "runtime_phase_started_at": utc_iso(phase_event.created_at) if phase_event else None,
                "runtime_phase_elapsed_seconds": phase_elapsed_seconds,
                "paper_writing_started_at": utc_iso(paper_start.created_at) if paper_start else None,
                "publication_elapsed_seconds": publication_elapsed_seconds,
                "runtime_stopped_at": utc_iso(stopped_at),
                "can_graceful_drain": (
                    run.status == "running"
                    and runtime_phase == "research"
                    and run.pause_requested_at is None
                ),
                "can_pause": run.status in {"running", "draining"} and run.pause_requested_at is None,
                "can_resume": run.status == "paused" and not pause_transport_pending,
                "pause_pending": pause_transport_pending or (
                    run.pause_requested_at is not None and run.status != "paused"
                ),
                "pause_reason": run.pause_reason,
                "paused_at": utc_iso(run.paused_at),
                "total_paused_seconds": float(run.total_paused_seconds or 0),
                "pre_pause_status": run.pre_pause_status,
                "pre_pause_phase": run.pre_pause_phase,
                "research_budget_seconds": budget_seconds,
                "research_elapsed_seconds": elapsed_seconds,
                "publication_estimate_seconds": int(
                    execution_policy.get("publication_estimate_seconds")
                    or self.settings.publication_estimate_seconds
                ),
                "usage": repo.run_usage_summary(user_id, run_id),
                "created_at": utc_iso(run.created_at),
                "started_at": utc_iso(run.started_at),
                "completed_at": utc_iso(run.completed_at),
                "attempts": [
                    {"attempt_id": item.attempt_id, "number": item.attempt_number, "status": item.status, "error": item.error}
                    for item in run.attempts
                ],
                "nodes": [self._node_payload(node) for node in nodes],
                "workers": [self._worker_payload(item) for item in workers],
                "assignments": [self._assignment_payload(item) for item in assignments],
                "verified_claims": verified_claims,
                "final_report_path": run.final_report_path,
                "final_paper_pdf_path": run.final_paper_pdf_path,
                "final_paper_tex_path": run.final_paper_tex_path,
                "final_paper_path": run.final_paper_pdf_path or run.final_report_path,
            }

    def verified_claim_proof(self, user_id: str, run_id: str, claim_id: str) -> dict[str, str]:
        """读取已核验 Claim 的完整证明；先核对 Run 所有权、Claim 归属和候选文件哈希，再提取固定论证章节。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.get_run(user_id, run_id)
            claim = session.get(VerifiedClaimRecord, claim_id)
            if claim is None or claim.run_id != run_id or claim.verdict != "PASS":
                raise FileNotFoundError("Verified Claim 不存在")
            if not claim.candidate_path:
                raise FileNotFoundError("该 Verified Claim 没有可用的完整证明")
            candidate_path = self.artifacts.resolve(claim.candidate_path)
            if not candidate_path.is_file():
                raise FileNotFoundError("该 Verified Claim 的完整证明文件不存在")
            if sha256_file(candidate_path) != claim.sha256:
                raise DynamicProverError("Verified Claim 完整证明文件的 SHA-256 与核验记录不一致")
            proof = self._extract_complete_argument(candidate_path.read_text(encoding="utf-8"))
            if not proof:
                raise DynamicProverError("Verified Claim Candidate 缺少 `完整论证` 章节")
            if claim.proof_sha256 and hashlib.sha256(proof.encode("utf-8")).hexdigest() != claim.proof_sha256:
                raise DynamicProverError("Verified Claim 完整论证的 SHA-256 与核验证书不一致")
            return {
                "verified_claim_id": claim.verified_claim_id,
                "proof_markdown": proof,
                "candidate_sha256": claim.candidate_sha256 or claim.sha256,
                "proof_sha256": claim.proof_sha256 or "",
                "verification_certificate_path": claim.certificate_path or "",
            }

    @staticmethod
    def _extract_complete_argument(candidate: str) -> str:
        """从当前中英文 Claim Candidate 契约中提取完整论证，并在下一个三级标题前结束。"""
        headings = list(re.finditer(r"^###\s+(.+?)\s*$", candidate, flags=re.MULTILINE))
        accepted = {"完整论证", "Complete Argument"}
        for index, heading in enumerate(headings):
            if heading.group(1).strip() not in accepted:
                continue
            start = heading.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(candidate)
            return candidate[start:end].strip()
        return ""

    @staticmethod
    def _pause_overlap_seconds(events: list[Any], start: Any, end: Any) -> float:
        """计算给定阶段内已完成暂停区间，避免论文计时把暂停时间算进去。"""
        paused_at = None
        total = 0.0
        for event in events:
            created = event.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if event.kind == "run_paused":
                paused_at = created
            elif event.kind == "run_resumed" and paused_at is not None:
                left = max(start, paused_at)
                right = min(end, created)
                if right > left:
                    total += (right - left).total_seconds()
                paused_at = None
        return total

    # ------------------------------------------------------------------ run scheduler
    async def _run(self, user_id: str, run_id: str, attempt_id: str) -> None:
        self._node_tasks.setdefault(run_id, {})
        self._run_stop_events.setdefault(run_id, asyncio.Event())
        try:
            with self.database.session() as session:
                initial = session.get(ResearchRunRecord, run_id)
                resuming_launch = bool(initial is not None and initial.status == "resuming")
            if resuming_launch:
                await self._wait_until_resumed(run_id)
            preflight = getattr(self.backend, "preflight_literature", None)
            if callable(preflight):
                try:
                    result = await preflight()
                except Exception as exc:
                    with self.database.session() as session:
                        RepositoryHub(session, self.artifacts).append_event(
                            "run",
                            run_id,
                            "literature_preflight_warning",
                            "文献检索预检暂不可用；Research Run 继续，文献任务按实际工具结果局部处理",
                            {"error": str(exc)},
                        )
                    result = None
                if result is not None:
                    with self.database.session() as session:
                        RepositoryHub(session, self.artifacts).append_event(
                            "run", run_id, "literature_preflight_passed",
                            "文献检索与 arXiv 原文链预检通过；尚未发生模型调用",
                            dict(result or {}),
                        )
            self._mark_run_started(user_id, run_id, attempt_id)
            with self.database.session() as session:
                run = RepositoryHub(session, self.artifacts).get_run(user_id, run_id)
                started = run.started_at or run.created_at
                duration_seconds = self._run_duration_seconds(run)
            self._materialize_initial_nodes(user_id, run_id)
            self._refresh_architecture_snapshot(run_id)
            self._restore_paused_architecture_review(run_id)
            while True:
                post_drain_closure = self._post_drain_closure_state(run_id)
                with self.database.session() as session:
                    persisted = session.get(ResearchRunRecord, run_id)
                    pause_requested = bool(
                        persisted is not None and persisted.pause_requested_at is not None
                    )
                    persisted_status = persisted.status if persisted is not None else "failed"
                    graceful_requested = bool(
                        persisted is not None and persisted.graceful_drain_requested_at is not None
                    )
                if pause_requested and persisted_status != "paused":
                    await self._enter_run_pause(
                        run_id,
                        reason=(persisted.pause_reason if persisted is not None else None) or "user",
                    )
                    await self._wait_until_resumed(run_id)
                    continue
                if persisted_status in {"paused", "resuming"}:
                    await self._wait_until_resumed(run_id)
                    continue
                if graceful_requested and post_drain_closure is None:
                    self._completion_requests[run_id] = "partially_resolved"
                    self._run_stop_events[run_id].set()
                else:
                    await self._schedule_ready_nodes(user_id, run_id, attempt_id)
                    self._maybe_schedule_architecture_review(run_id)
                if self._run_stop_events[run_id].is_set():
                    if run_id in self._deletion_runs:
                        await self._abort_run_for_deletion(user_id, run_id, attempt_id)
                    elif run_id in self._shutdown_runs:
                        await self._drain_run(run_id, "MathLab 服务停止")
                        self._mark_attempt_interrupted(user_id, run_id, attempt_id, "MathLab 服务停止")
                    else:
                        completion_kind = self._completion_requests.pop(run_id, "partially_resolved")
                        if post_drain_closure is None:
                            source = (
                                "user_graceful_drain"
                                if graceful_requested else "reviewer_complete_run"
                            )
                            await self._drain_run(run_id, "收到结束研究请求，开始分层收束")
                            self._enter_post_drain_closure(
                                run_id,
                                attempt_id,
                                source=source,
                                preferred_completion_kind=completion_kind,
                            )
                            continue
                        await self._drain_run(
                            run_id, "最终全局 Reviewer 已确认完成，准备论文写作"
                        )
                        self._authorize_publication(
                            run_id,
                            attempt_id,
                            completion_kind,
                            source="reviewer_complete_run",
                        )
                        await self._finalize_run(
                            user_id, run_id, attempt_id,
                            completion_kind=completion_kind,
                        )
                        return
                    return
                if run_id in self._capacity_holds:
                    await self._await_capacity_hold(run_id)
                    continue
                if (
                    post_drain_closure is None
                    and self._run_research_elapsed_seconds(run_id, started) >= duration_seconds
                ):
                    await self._drain_run(run_id, "Research Run 墙钟/预算上限已达到")
                    self._enter_post_drain_closure(
                        run_id,
                        attempt_id,
                        source="budget_exhausted",
                        preferred_completion_kind="budget_exhausted",
                    )
                    continue
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    run = repo.get_run(user_id, run_id)
                    if run.status in {"completed", "failed", "interrupted"}:
                        return
                    remaining = [node for node in repo.list_nodes(run_id) if node.status in {"ready", "active", "draining"}]
                running = [task for task in self._node_tasks.get(run_id, {}).values() if not task.done()]
                architecture_running = self._architecture_tasks.get(run_id)
                if not remaining and not running and not (architecture_running and not architecture_running.done()):
                    reasons = self._architecture_trigger_reasons(run_id)
                    if not reasons:
                        self._queue_closure_checkpoint(run_id)
                    self._maybe_schedule_architecture_review(run_id, force=True)
                    await asyncio.sleep(0.2)
                    continue
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            with self.database.session() as session:
                paused = bool(
                    (run := session.get(ResearchRunRecord, run_id)) is not None
                    and run.status == "paused"
                )
            if not paused:
                self._mark_attempt_interrupted(user_id, run_id, attempt_id, "Run task 被取消")
            raise
        except Exception as exc:
            await self._cancel_run_children(run_id)
            self._mark_run_failed(user_id, run_id, attempt_id, str(exc))
        finally:
            self._deletion_runs.discard(run_id)

    def _restore_paused_architecture_review(self, run_id: str) -> None:
        """跨重启恢复同一次 fresh Reviewer operation；下一次 Review 仍另开 thread。"""
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            operation = session.scalar(
                select(AgentOperationCheckpointRecord)
                .where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.operation_kind == "architecture_review",
                    AgentOperationCheckpointRecord.status == "paused",
                    AgentOperationCheckpointRecord.pause_epoch == run.pause_epoch,
                )
                .order_by(AgentOperationCheckpointRecord.created_at.desc())
                .limit(1)
            )
            if operation is None:
                return
        self._ensure_architecture_task(run_id)

    async def _abort_run_for_deletion(self, user_id: str, run_id: str, attempt_id: str) -> None:
        """立即取消研究子任务并记为 interrupted；删除语义禁止再调用 Writer。"""
        tasks = [task for task in self._node_tasks.get(run_id, {}).values() if not task.done()]
        for node_id in self._node_tasks.get(run_id, {}):
            tasks.extend(task for task in self._worker_tasks.get(node_id, {}).values() if not task.done())
        architecture = self._architecture_tasks.get(run_id)
        if architecture is not None and not architecture.done():
            tasks.append(architecture)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._mark_attempt_interrupted(user_id, run_id, attempt_id, "目标正在永久删除；已终止且不生成 Final Writer 报告")

    def _mark_run_deleted_interrupted(self, user_id: str, run_id: str) -> None:
        """处理尚未由本进程启动的 queued Run，保留 usage/credit 结算后交给删除任务。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status == "completed":
                return
            run.status = "interrupted"; run.terminal_at = utc_now(); run.claimed_by_runner = None; run.claimed_at = None
            attempt = run.attempts[-1] if run.attempts else None
            if attempt and attempt.status != "completed":
                attempt.status = "interrupted"; attempt.error = "目标正在永久删除"; attempt.completed_at = utc_now()
            repo.settle_run_cost(run_id)

    async def _schedule_ready_nodes(self, user_id: str, run_id: str, attempt_id: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            nodes = repo.list_nodes(run_id)
            active = [node for node in nodes if node.status in {"active", "draining"}]
            capacity = max(0, self.settings.max_parallel_research_nodes - len(active))
            ready = [node for node in nodes if node.status == "ready"][:capacity]
            ready_ids = [node.node_id for node in ready]
            paused_node_ids = set(session.scalars(
                select(AgentOperationCheckpointRecord.node_id).where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.status == "paused",
                    AgentOperationCheckpointRecord.pause_epoch == run.pause_epoch,
                    AgentOperationCheckpointRecord.node_id.is_not(None),
                )
            ))
            recovery_ids = [
                node.node_id for node in active
                if node.node_id in paused_node_ids
                and node.node_id not in self._node_tasks.get(run_id, {})
            ]
        for node_id in recovery_ids:
            self._launch_node(user_id, run_id, attempt_id, node_id, recover=True)
        for node_id in ready_ids:
            self._launch_node(user_id, run_id, attempt_id, node_id)

    def _launch_node(self, user_id: str, run_id: str, attempt_id: str, node_id: str, *, recover: bool = False) -> None:
        tasks = self._node_tasks.setdefault(run_id, {})
        existing = tasks.get(node_id)
        if existing is not None and not existing.done():
            return
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None or node.status not in ({"active", "draining"} if recover else {"ready"}):
                return
            if not recover:
                active_count = int(session.scalar(select(func.count()).select_from(ResearchNodeRecord).where(
                    ResearchNodeRecord.run_id == run_id,
                    ResearchNodeRecord.status.in_(["active", "draining"]),
                )) or 0)
                if active_count >= self.settings.max_parallel_research_nodes:
                    return
                node.status = "active"
                node.node_version += 1
        task = asyncio.create_task(self._run_node(user_id, run_id, attempt_id, node_id), name=f"mathlab-node-{node_id}")
        tasks[node_id] = task
        self._worker_tasks.setdefault(node_id, {})
        self._node_draining.setdefault(node_id, asyncio.Event())
        task.add_done_callback(lambda _: tasks.pop(node_id, None))

    # ------------------------------------------------------------------ local node loop
    async def _run_node(self, user_id: str, run_id: str, attempt_id: str, node_id: str) -> None:
        local_reviews = self._durable_local_review_count(node_id)
        latest_decision: SupervisorDecision | None = None
        verification_feedback = ""
        report_buffer, report_ids = self._pending_worker_report_inbox(node_id)
        try:
            initial_node = self._get_node(node_id)
            resuming = int((initial_node.details or {}).get("resume_count") or 0) > 0
            assembly_only = self._is_post_drain_final_claim(run_id, initial_node)
            paused_operations = self._paused_node_operations(node_id)
            worker_operations = [
                item for item in paused_operations if item.operation_kind == "worker_assignment"
            ] if not assembly_only else []
            supervisor_operation = next(
                (item for item in paused_operations if item.operation_kind.startswith("supervisor_")),
                None,
            )
            verifier_operation = next(
                (item for item in paused_operations if item.operation_kind == "claim_verification"),
                None,
            )
            if worker_operations:
                self._prepare_paused_worker_assignments(worker_operations)
                self._launch_ready_assignments(user_id, run_id, attempt_id, node_id)

            candidate: tuple[str, Path] | None = None
            if supervisor_operation is not None:
                payload = dict(supervisor_operation.payload or {})
                bootstrap = bool(payload.get("bootstrap")) and not assembly_only
                report_buffer, report_ids = self._pending_worker_report_inbox(node_id)
                durable_feedback, feedback_ids = self._pending_verification_feedback(node_id)
                combined_feedback = "\n\n".join(filter(None, [
                    str(payload.get("verification_feedback") or ""), durable_feedback,
                ]))
                latest_decision = await self._supervisor_review(
                    user_id, run_id, attempt_id, node_id,
                    bootstrap=bootstrap,
                    resuming=bool(payload.get("resuming")),
                    reports=report_buffer,
                    verification_feedback=combined_feedback,
                )
                candidate = self._apply_supervisor_decision(
                    user_id, run_id, node_id, latest_decision, bootstrap=bootstrap,
                    consumed_report_ids=report_ids,
                    consumed_feedback_ids=feedback_ids,
                )
                local_reviews = self._durable_local_review_count(node_id)
                report_buffer, report_ids = [], []
            elif not worker_operations and verifier_operation is None:
                report_buffer, report_ids = self._pending_worker_report_inbox(node_id)
                durable_feedback, feedback_ids = self._pending_verification_feedback(node_id)
                resume_feedback = (
                    "全局 Reviewer 已明确恢复此 blocked Node。复用原 Supervisor 和尚未退休的 Worker identities，"
                    "根据最新 Architecture Directive 分配下一轮可立即执行的任务。"
                    if resuming else ""
                )
                latest_decision = await self._supervisor_review(
                    user_id, run_id, attempt_id, node_id,
                    bootstrap=not resuming and not assembly_only,
                    resuming=resuming,
                    reports=report_buffer,
                    verification_feedback="\n\n".join(filter(None, [resume_feedback, durable_feedback])),
                )
                candidate = self._apply_supervisor_decision(
                    user_id, run_id, node_id, latest_decision,
                    bootstrap=not resuming and not assembly_only,
                    consumed_report_ids=report_ids,
                    consumed_feedback_ids=feedback_ids,
                )
                local_reviews = self._durable_local_review_count(node_id)
                report_buffer, report_ids = [], []
            if verifier_operation is not None:
                payload = dict(verifier_operation.payload or {})
                candidate_id = str(payload.get("candidate_id") or "")
                candidate_path = self.artifacts.resolve(str(payload.get("candidate_path") or ""))
                if candidate_id and candidate_path.is_file():
                    candidate = (candidate_id, candidate_path)
            if candidate:
                verdict, verification_error, newly_verified = await self._verify_candidate_safely(
                    user_id, run_id, attempt_id, node_id, candidate[0], candidate[1]
                )
                if verdict is not None and verdict.verdict == "PASS":
                    node_kind = self._get_node(node_id).kind
                    completed = (
                        await self._complete_node_after_verification(
                            run_id, node_id, candidate[0]
                        )
                        if newly_verified or node_kind == "claim"
                        else False
                    )
                    if completed:
                        return
                    verification_feedback = verdict.raw_markdown
                verification_feedback = (
                    verification_feedback
                    or (
                        verdict.raw_markdown if verdict is not None
                        else f"Fresh verification 暂不可用：{verification_error}"
                    )
                )

            while True:
                node = self._get_node(node_id)
                if node.status in TERMINAL_NODE_STATES:
                    return
                if node.status == "draining" or self._node_draining[node_id].is_set():
                    await self._drain_node(user_id, run_id, attempt_id, node_id, target_status=(node.details or {}).get("drain_target_status", "retired"))
                    return
                if local_reviews >= self.settings.max_local_reviews_per_node:
                    self._request_node_drain(node_id, "blocked", "Local Review safety fuse reached")
                    continue

                self._launch_ready_assignments(user_id, run_id, attempt_id, node_id)
                worker_tasks = self._worker_tasks.setdefault(node_id, {})
                active_tasks = [task for task in worker_tasks.values() if not task.done()]
                if not active_tasks:
                    if latest_decision and latest_decision.node_status == "节点完成" and node.kind == "discovery":
                        late_reports, late_report_ids = self._pending_worker_report_inbox(node_id)
                        if not late_report_ids:
                            self._freeze_final_stage_from_decision(run_id, node_id, latest_decision)
                            self._set_node_status(node_id, "completed")
                            await self._emit_stage(run_id, {"node_id": node_id, "kind": "discovery_completed", "major": latest_decision.major_event})
                            return
                        report_buffer, report_ids = late_reports, late_report_ids
                        latest_decision = None
                    if latest_decision and latest_decision.node_status == "阻塞":
                        late_reports, late_report_ids = self._pending_worker_report_inbox(node_id)
                        if not late_report_ids:
                            self._freeze_final_stage_from_decision(run_id, node_id, latest_decision)
                            self._set_node_status(node_id, "blocked")
                            await self._emit_stage(run_id, {"node_id": node_id, "kind": "node_blocked", "major": True})
                            return
                        report_buffer, report_ids = late_reports, late_report_ids
                        latest_decision = None
                    # Give the supervisor one bounded opportunity to assign idle workers.
                    if local_reviews >= self.settings.max_local_reviews_per_node:
                        self._request_node_drain(node_id, "blocked", "No executable assignments")
                        continue
                    report_buffer, report_ids = self._pending_worker_report_inbox(node_id)
                    durable_feedback, feedback_ids = self._pending_verification_feedback(node_id)
                    latest_decision = await self._supervisor_review(
                        user_id, run_id, attempt_id, node_id,
                        bootstrap=False,
                        reports=report_buffer,
                        verification_feedback="\n\n".join(filter(None, [
                            verification_feedback, durable_feedback,
                        ])),
                    )
                    if latest_decision.node_status == "节点完成" and node.kind == "discovery":
                        pending_reports, pending_report_ids = self._pending_worker_report_inbox(node_id)
                        reviewed_ids = set(report_ids)
                        if any(item not in reviewed_ids for item in pending_report_ids):
                            report_buffer, report_ids = pending_reports, pending_report_ids
                            latest_decision = None
                            continue
                    fuse_reached = (
                        local_reviews + 1 >= self.settings.max_local_reviews_per_node
                    )
                    effective_decision = (
                        self._suppress_new_assignments(latest_decision)
                        if fuse_reached else latest_decision
                    )
                    candidate = self._apply_supervisor_decision(
                        user_id, run_id, node_id, effective_decision, bootstrap=False,
                        consumed_report_ids=report_ids,
                        consumed_feedback_ids=feedback_ids,
                    )
                    local_reviews = self._durable_local_review_count(node_id)
                    report_buffer, report_ids = [], []
                    verification_feedback = ""
                    stage_event = {
                        "node_id": node_id,
                        "kind": "local_review",
                        "major": latest_decision.major_event,
                        "node_status": latest_decision.node_status,
                    }
                    if candidate:
                        completed, verification_feedback, stage_event = await self._review_candidate_verification(
                            user_id, run_id, attempt_id, node_id, candidate,
                            major=latest_decision.major_event,
                        )
                        if completed:
                            return
                    await self._emit_stage(run_id, stage_event)
                    if fuse_reached:
                        self._request_node_drain(node_id, "blocked", "Local Review safety fuse reached")
                        continue
                    if not self._ready_assignments(node_id) and not [task for task in worker_tasks.values() if not task.done()]:
                        late_reports, late_report_ids = self._pending_worker_report_inbox(node_id)
                        if late_report_ids:
                            report_buffer, report_ids = late_reports, late_report_ids
                            latest_decision = None
                            continue
                        if latest_decision.node_status == "节点完成" and node.kind == "discovery":
                            self._freeze_final_stage_from_decision(run_id, node_id, latest_decision)
                            self._set_node_status(node_id, "completed")
                        else:
                            if latest_decision is not None:
                                self._freeze_final_stage_from_decision(run_id, node_id, latest_decision)
                            self._set_node_status(node_id, "blocked")
                        return
                    continue

                done, _ = await asyncio.wait(active_tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue
                major = False
                for task in done:
                    worker_id = next((wid for wid, current in worker_tasks.items() if current is task), "")
                    if worker_id:
                        worker_tasks.pop(worker_id, None)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:
                        self._mark_worker_task_failed(worker_id, str(exc))
                        continue
                    major = major or result[1].major_event
                report_buffer, report_ids = self._pending_worker_report_inbox(node_id)
                still_running = [task for task in worker_tasks.values() if not task.done()]
                if len(report_buffer) < self.settings.local_review_report_threshold and not major and still_running:
                    continue

                if local_reviews >= self.settings.max_local_reviews_per_node:
                    self._request_node_drain(node_id, "blocked", "Local Review safety fuse reached")
                    continue
                durable_feedback, feedback_ids = self._pending_verification_feedback(node_id)
                latest_decision = await self._supervisor_review(
                    user_id, run_id, attempt_id, node_id,
                    bootstrap=False,
                    reports=report_buffer,
                    verification_feedback="\n\n".join(filter(None, [
                        verification_feedback, durable_feedback,
                    ])),
                )
                if latest_decision.node_status == "节点完成" and node.kind == "discovery":
                    await self._cancel_node_workers(node_id)
                    pending_reports, pending_report_ids = self._pending_worker_report_inbox(node_id)
                    reviewed_ids = set(report_ids)
                    if any(item not in reviewed_ids for item in pending_report_ids):
                        report_buffer, report_ids = pending_reports, pending_report_ids
                        latest_decision = None
                        continue
                fuse_reached = (
                    local_reviews + 1 >= self.settings.max_local_reviews_per_node
                )
                effective_decision = self._suppress_new_assignments(latest_decision) if fuse_reached else latest_decision
                candidate = self._apply_supervisor_decision(
                    user_id, run_id, node_id, effective_decision, bootstrap=False,
                    consumed_report_ids=report_ids,
                    consumed_feedback_ids=feedback_ids,
                )
                local_reviews = self._durable_local_review_count(node_id)
                report_buffer, report_ids = [], []
                verification_feedback = ""
                if candidate:
                    completed, verification_feedback, stage_event = await self._review_candidate_verification(
                        user_id, run_id, attempt_id, node_id, candidate,
                        major=latest_decision.major_event,
                    )
                    if completed:
                        return
                    await self._emit_stage(run_id, stage_event)
                else:
                    await self._emit_stage(run_id, {"node_id": node_id, "kind": "local_review", "major": latest_decision.major_event, "node_status": latest_decision.node_status})
                if fuse_reached:
                    self._request_node_drain(node_id, "blocked", "Local Review safety fuse reached")
                    continue
                if latest_decision.node_status == "节点完成" and node.kind == "discovery":
                    await self._cancel_node_workers(node_id)
                    late_reports, late_report_ids = self._pending_worker_report_inbox(node_id)
                    if late_report_ids:
                        report_buffer, report_ids = late_reports, late_report_ids
                        latest_decision = None
                        continue
                    self._freeze_final_stage_from_decision(run_id, node_id, latest_decision)
                    self._set_node_status(node_id, "completed")
                    return
                if latest_decision.node_status == "进入收束":
                    self._request_node_drain(node_id, "retired", "Supervisor requested draining")
                elif latest_decision.node_status == "阻塞":
                    self._request_node_drain(node_id, "blocked", "Supervisor declared structural block")
        except asyncio.CancelledError:
            if not self._run_is_paused(run_id):
                self._request_node_drain(node_id, "retired", "Node task cancelled")
            raise
        except RoleContractError as exc:
            await self._cancel_node_workers(node_id)
            if exc.role_key != "node_supervisor":
                self._set_node_status(node_id, "failed", error=str(exc))
                await self._emit_stage(
                    run_id,
                    {"node_id": node_id, "kind": "node_failed", "major": True, "error": str(exc)},
                )
                return
            self._set_node_status(node_id, "blocked", error=str(exc))
            await self._emit_stage(
                run_id,
                {"node_id": node_id, "kind": "node_blocked", "major": True, "error": str(exc)},
            )
        except Exception as exc:
            await self._cancel_node_workers(node_id)
            self._set_node_status(node_id, "failed", error=str(exc))
            await self._emit_stage(run_id, {"node_id": node_id, "kind": "node_failed", "major": True, "error": str(exc)})

    def _paused_node_operations(self, node_id: str) -> list[Any]:
        """读取 Node 在进程退出前冻结的业务调用。"""
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            run = session.get(ResearchRunRecord, node.run_id) if node is not None else None
            if run is None:
                return []
            return [
                self._detach_operation(item)
                for item in session.scalars(
                    select(AgentOperationCheckpointRecord)
                    .where(
                        AgentOperationCheckpointRecord.node_id == node_id,
                        AgentOperationCheckpointRecord.status == "paused",
                        AgentOperationCheckpointRecord.pause_epoch == run.pause_epoch,
                    )
                    .order_by(AgentOperationCheckpointRecord.created_at)
                )
            ]

    def _prepare_paused_worker_assignments(self, operations: list[Any]) -> None:
        """跨重启时把暂停中的 Assignment 恢复为可调度，但不创建新版本。"""
        with self.database.session() as session:
            for operation in operations:
                assignment_id = str((operation.payload or {}).get("assignment_id") or "")
                assignment = session.get(WorkerAssignmentRecord, assignment_id)
                if assignment is None or assignment.status not in {"running", "ready"}:
                    continue
                assignment.status = "ready"
                assignment.completed_at = None
                worker = session.get(WorkerIdentityRecord, assignment.worker_id)
                if worker is not None and worker.status not in {"retired", "draining"}:
                    worker.status = "assigned"

    def _launch_ready_assignments(self, user_id: str, run_id: str, attempt_id: str, node_id: str) -> None:
        if self._node_draining.setdefault(node_id, asyncio.Event()).is_set():
            return
        if self._is_post_drain_final_claim(run_id, self._get_node(node_id)):
            return
        mapping = self._worker_tasks.setdefault(node_id, {})
        for assignment in self._ready_assignments(node_id):
            if assignment.worker_id in mapping and not mapping[assignment.worker_id].done():
                continue
            if self._get_node(node_id).status != "active":
                return
            worker = self._get_worker(assignment.worker_id)
            if worker.status not in {"assigned", "idle"}:
                continue
            task = asyncio.create_task(
                self._run_worker(user_id, run_id, attempt_id, node_id, assignment.worker_id, assignment.assignment_id),
                name=f"mathlab-worker-{assignment.worker_id}-v{assignment.assignment_version}",
            )
            mapping[assignment.worker_id] = task

    async def _run_worker(self, user_id: str, run_id: str, attempt_id: str, node_id: str, worker_id: str, assignment_id: str) -> tuple[str, WorkerReport]:
        assignment, worker, node, contract, claims, sources = self._worker_context(user_id, run_id, node_id, worker_id, assignment_id)
        self._mark_assignment_running(assignment_id, worker_id)
        role = PERSONA_TO_ROLE[worker.persona]
        task = f"""# 本轮 Worker Assignment

## Research Contract Boundary

{contract}

## Current Research Node

Node ID: {node.node_id}
Node Kind: {node.kind}
Title: {node.title}
Objective:
{node.objective}

Completion Criterion:
{node.completion_criterion}

Controlling Proposition ID:
{self._controlling_proposition_id(node.node_id) if node.kind == 'claim' else '不适用；Discovery Candidate 只能标记“辅助事实”'}

## Persistent Worker Identity

Worker ID: {worker.worker_id}
Worker Label: {worker.worker_label}
Persona: {worker.persona}
Assignment Version: {assignment.assignment_version}

你是本 Node 内的长期研究者。若这是同一 thread 的后续任务，应利用已有研究记忆；但过去未验证的猜测仍不是 Verified Fact。

## Your Exact Task

{assignment.task_markdown}

## Verified Claims Available as Black Boxes

{claims or '无。'}

提交结构清楚的数学 Markdown。只保留唯一 `## 任务状态` 硬契约；重大事件可用 `## 重大事件`。未完成时仍需交付当前最强结果、精确缺口、失败机制和下一步。
"""
        try:
            literature_root = (
                self._node_root(run_id, node_id)
                / "literature"
                / worker_id
                / f"assignment-v{assignment.assignment_version:03d}"
            )
            report, turn = await self._validated_role(
                user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=node_id,
                role=role, role_key=f"{role}:{worker.worker_id}", scope_type="worker",
                scope_id=worker.worker_id, task=task, parser=parse_worker_report,
                persistent=True,
                literature_access=worker.persona == "literature",
                literature_archive_root=literature_root if worker.persona == "literature" else None,
                context_paths=sources,
                operation_kind="worker_assignment",
                checkpoint_payload={
                    "assignment_id": assignment_id,
                    "worker_id": worker_id,
                    "assignment_version": assignment.assignment_version,
                },
            )
        except asyncio.CancelledError:
            if not self._run_is_paused(run_id):
                self._mark_assignment_interrupted(assignment_id, worker_id)
            raise
        if worker.persona == "literature" and turn.evidence:
            self._publish_literature_evidence(
                run_id, node_id, worker_id, assignment.assignment_version, turn.evidence
            )
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            current_assignment = session.get(WorkerAssignmentRecord, assignment_id)
            current_node = session.get(ResearchNodeRecord, node_id)
            run = session.get(ResearchRunRecord, run_id)
            if current_assignment is None or current_node is None or run is None:
                raise DynamicProverError("Worker 返回时 assignment/node/run 已不存在")
            path = self._node_root(run_id, node_id) / "workers" / worker_id / f"assignment-v{current_assignment.assignment_version:03d}-report.md"
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), report.raw_markdown)
            repo.add_worker_report(
                current_assignment, status=report.status, major_event=report.major_event,
                markdown_path=path, node_version=current_node.node_version, graph_version=run.graph_version,
            )
            repo.append_event("node", node_id, "worker_report", f"{current_assignment.worker_label} 已回报", {
                "worker_id": worker_id, "assignment_id": assignment_id,
                "assignment_version": current_assignment.assignment_version,
                "status": report.status, "major_event": report.major_event,
            })
        return assignment_id, report

    # ------------------------------------------------------------------ supervisor
    async def _supervisor_review(self, user_id: str, run_id: str, attempt_id: str, node_id: str, *, bootstrap: bool, reports: list[tuple[str, WorkerReport]], verification_feedback: str, resuming: bool = False) -> SupervisorDecision:
        node, contract, claims, worker_state, directives, notes, relations, resources = self._node_context(user_id, run_id, node_id)
        assembly_only = self._is_post_drain_final_claim(run_id, node)
        final_stages = (
            self._final_local_stages_markdown(run_id, exclude_node_id=node_id)
            if assembly_only else ""
        )
        mode = "首轮初始化" if bootstrap else ("blocked Node 恢复研究" if resuming else "异步 Local Supervisor Review")
        task = f"""# 本轮任务：{mode}

## Research Contract

{contract}

## Current Research Node

Node ID: {node.node_id}
Node Kind: {node.kind}
Title: {node.title}
Objective:
{node.objective}

Completion Criterion:
{node.completion_criterion}

Controlling Proposition ID:
{self._controlling_proposition_id(node.node_id) if node.kind == 'claim' else '不适用；Discovery Node 没有控制命题 ID'}

Controlling Proposition Statement:
{node.objective if node.kind == 'claim' else '不适用；Discovery Node 由 Objective 与 Completion Criterion 定义局部责任'}

## Parent and Graph Relations

{relations or '当前没有已登记的父节点或直接 Graph edge。'}

## Current Architecture Directives

{directives or '无新的上层指令。'}

## Your Persistent Notes

{notes or '暂无。'}

## Verified Claims

{claims or '无。'}

## Final Local Stages For Final Claim Assembly

{final_stages or ('无。' if assembly_only else '不适用；当前不是 post-drain Final Claim assembly。')}

## New Worker Reports

{self._report_context(reports) or '首轮初始化尚无 Worker 报告。'}

## Latest Verification Feedback

{verification_feedback or '无。'}

## Current Worker Pool and Assignments

{worker_state or '无。'}

## Local Resource Boundary

{resources}

请提交完整 Node Supervisor Markdown。Worker 是长期学生身份：任务连续时优先复用既有 label/thread；不要建立 pending backlog；每次只给当前 idle Worker 或确有空余 slot 的新 Worker 分配立即执行任务。首轮覆盖四种 Persona；全局 Graph 只能提名，不能直接修改。Mathematical Significance、Promotion Nomination 与 Architecture Attention 是三个独立维度；普通 major 进展、Candidate assembly、Fresh Verifier 修复与 Node completion 默认在本地消化。
{"这是 post-drain Final Claim assembly：`## Worker Assignments` 必须写 `无。`，不得创建或恢复任何 Worker；直接用 Contract、Verified Claims、Final Local Stages 与最新 Verifier feedback 组装或修订 controlling Claim Candidate。" if assembly_only else ""}
"""
        def parser(text: str) -> tuple[SupervisorDecision | None, ValidationResult]:
            parsed, validation = parse_supervisor(
                text,
                bootstrap=bootstrap,
                max_workers=self.settings.max_workers_per_research_node,
            )
            errors = list(validation.errors)
            if parsed is not None and node.kind == "claim" and parsed.node_status == "节点完成":
                errors.append("Claim Node 不能由 Supervisor 标记“节点完成”；必须提交 Candidate 并通过 fresh Verifier")
            candidate_validation = validate_supervisor_candidate(
                text,
                node_kind=node.kind,
                controlling_proposition_id=self._controlling_proposition_id(node.node_id),
            )
            errors.extend(candidate_validation.errors)
            return parsed, ValidationResult(
                list(dict.fromkeys(errors)),
                list(dict.fromkeys([*validation.warnings, *candidate_validation.warnings])),
            )
        decision, _ = await self._validated_role(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=node_id,
            role="node_supervisor", role_key="node_supervisor", scope_type="node", scope_id=node_id,
            task=task, parser=parser, persistent=True,
            operation_kind=(
                "supervisor_bootstrap" if bootstrap
                else ("supervisor_resume" if resuming else "supervisor_review")
            ),
            checkpoint_payload={
                "node_id": node_id,
                "bootstrap": bootstrap,
                "resuming": resuming,
                "verification_feedback": verification_feedback,
                "worker_assignment_ids": [item[0] for item in reports],
            },
        )
        path = self._node_root(run_id, node_id) / "supervisor" / f"review-{self._next_local_review_number(node_id):03d}.md"
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), decision.raw_markdown)
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is not None:
                details = dict(node.details or {})
                details["latest_supervisor_review_path"] = self.artifacts.reference(path)
                details["latest_supervisor_review_sha256"] = sha256_file(path)
                node.details = details
        return decision

    def _apply_supervisor_decision(
        self,
        user_id: str,
        run_id: str,
        node_id: str,
        decision: SupervisorDecision,
        *,
        bootstrap: bool,
        consumed_report_ids: list[str] | None = None,
        consumed_feedback_ids: list[str] | None = None,
        counts_toward_fuse: bool = True,
    ) -> tuple[str, Path] | None:
        if self._is_post_drain_final_claim(run_id, self._get_node(node_id)):
            decision = self._suppress_new_assignments(decision)
            bootstrap = False
        candidate_result: tuple[str, Path] | None = None
        public_source_key: str | None = None
        root_relevant = False
        candidate_identity = None
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                raise DynamicProverError("Node 不存在")
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            details = dict(node.details or {})
            local_review_id = new_id("local_review")
            old_summary = {
                "status": node.status,
                "diagnosis": str(details.get("latest_supervisor_diagnosis") or ""),
            }
            details["latest_supervisor_diagnosis"] = decision.diagnosis
            if decision.supervisor_notes:
                details["supervisor_notes"] = decision.supervisor_notes

            workers = repo.list_workers(node_id)
            active_workers = [worker for worker in workers if worker.status != "retired"]
            by_label = {worker.worker_label: worker for worker in workers}

            for label in decision.retire_worker_labels:
                worker = by_label.get(label)
                if worker is None or worker.status == "retired":
                    continue
                # Do not break minimum persona coverage while the Node remains active.
                same_persona = [item for item in active_workers if item.persona == worker.persona and item.worker_id != worker.worker_id and item.status != "retired"]
                if not same_persona and node.status == "active":
                    details.setdefault("ignored_worker_retirements", []).append({"worker_label": label, "reason": "minimum persona coverage"})
                    continue
                if worker.status == "running":
                    # Let the in-flight assignment finish, then retire the persistent identity.
                    worker.status = "draining"
                    details.setdefault("deferred_worker_retirements", []).append(label)
                elif worker.status == "assigned":
                    current = repo.current_assignment(worker.worker_id)
                    if current is not None:
                        current.status = "cancelled"
                        current.completed_at = utc_now()
                    repo.retire_worker(worker.worker_id)
                else:
                    repo.retire_worker(worker.worker_id)

            for spec in decision.assignments:
                worker = by_label.get(spec.worker_label)
                if worker is None:
                    current_count = len([item for item in repo.list_workers(node_id) if item.status != "retired"])
                    if current_count >= self.settings.max_workers_per_research_node:
                        details.setdefault("ignored_assignments", []).append({"worker_label": spec.worker_label, "reason": "worker cap"})
                        continue
                    worker = repo.create_worker(node, persona=spec.persona, worker_label=spec.worker_label)
                    by_label[spec.worker_label] = worker
                elif worker.persona != spec.persona:
                    details.setdefault("ignored_assignments", []).append({"worker_label": spec.worker_label, "reason": "persona mismatch"})
                    continue
                if worker.status in {"running", "assigned", "draining", "retired"} or repo.current_assignment(worker.worker_id) is not None:
                    details.setdefault("ignored_assignments", []).append({"worker_label": spec.worker_label, "reason": f"worker busy:{worker.status}"})
                    continue
                repo.create_assignment(node, worker=worker, task_markdown=spec.task)

            if bootstrap:
                personas = {worker.persona for worker in repo.list_workers(node_id, include_retired=False)}
                missing = set(PERSONA_TO_ROLE) - personas
                if missing:
                    raise DynamicProverError("Bootstrap 未建立完整四 Persona Worker pool：" + ",".join(sorted(missing)))

            finding_ids: list[str] = []
            gap_key: str | None = None
            for finding in decision.findings:
                created = repo.add_finding(
                    node, finding_type=finding.finding_type, title=finding.title,
                    markdown=finding.markdown, significance=finding.significance,
                    nominated=finding.nominate, rationale=finding.rationale,
                    recommended_actions=[{"text": item} for item in finding.recommended_actions],
                )
                finding_ids.append(created.finding_id)
                if finding.finding_type == "missing_bridge":
                    gap_key = re.sub(r"\s+", " ", finding.title.strip().lower())[:300]
            if decision.claim_candidate:
                controlling_id = self._controlling_proposition_id(node.node_id)
                candidate_identity, candidate_validation = parse_candidate_identity(
                    decision.claim_candidate,
                    node_kind=node.kind,
                    controlling_proposition_id=controlling_id,
                )
                if not candidate_validation.ok or candidate_identity is None:
                    raise DynamicProverError(candidate_validation.repair_message())
                candidate_id = new_id("candidate")
                path = self._node_root(run_id, node_id) / "candidates" / f"{candidate_id}.md"
                self.artifacts.write_text(path.relative_to(self.artifacts.data_root), decision.claim_candidate)
                candidates = dict(details.get("candidate_artifacts") or {})
                candidates[candidate_id] = {
                    "path": self.artifacts.reference(path),
                    "sha256": sha256_file(path),
                    "canonical_statement": candidate_identity.statement,
                    "statement_sha256": hashlib.sha256(
                        candidate_identity.statement.encode("utf-8")
                    ).hexdigest(),
                    "proof_sha256": hashlib.sha256(
                        candidate_identity.proof.encode("utf-8")
                    ).hexdigest(),
                    "candidate_scope": candidate_identity.scope,
                    "controlling_proposition_id": (
                        candidate_identity.controlling_proposition_id or None
                    ),
                    "created_at": utc_now().isoformat(),
                }
                details["candidate_artifacts"] = candidates
                details["latest_candidate_artifact_id"] = candidate_id
                candidate_result = candidate_id, path
                root_relevant = candidate_identity.scope == "controlling_node_claim"
            strongest_finding = next((
                item for item in decision.findings
                if item.finding_type in {
                    "candidate_answer", "possible_claim", "proof_mechanism",
                    "structural_obstruction", "unexpected_reduction",
                }
            ), None)
            decisive_gap_finding = next((
                item for item in decision.findings if item.finding_type == "missing_bridge"
            ), None)
            details["strongest_current_result"] = (
                candidate_identity.statement
                if candidate_identity is not None
                else (
                    strongest_finding.markdown if strongest_finding is not None
                    else str(details.get("strongest_current_result") or "")
                )
            )
            details["minimal_sufficient_target"] = str(
                details.get("minimal_sufficient_target") or node.completion_criterion
            )
            details["decisive_gap"] = (
                decisive_gap_finding.markdown
                if decisive_gap_finding is not None
                else str(details.get("decisive_gap") or "")
            )
            details["why_node_is_still_active"] = (
                f"当前仍需闭合 decisive gap：{decisive_gap_finding.title}"
                if decisive_gap_finding is not None
                else (
                    "当前 Candidate 尚需 fresh verification 或局部修复。"
                    if candidate_identity is not None
                    else "仍有已分配的精确局部研究责任。"
                )
            )
            if not bootstrap and counts_toward_fuse:
                node.local_review_count = int(node.local_review_count or 0) + 1
            repo.consume_worker_reports(
                node_id,
                list(consumed_report_ids or []),
                supervisor_review_id=local_review_id,
            )
            feedback_ids = set(consumed_feedback_ids or [])
            if feedback_ids:
                inbox = list(details.get("verification_feedback_inbox") or [])
                now = utc_now().isoformat()
                for item in inbox:
                    payload = dict(item)
                    if str(payload.get("feedback_id") or "") in feedback_ids:
                        payload["status"] = "consumed"
                        payload["consumed_by_supervisor_review_id"] = local_review_id
                        payload["consumed_at"] = now
                        item.clear()
                        item.update(payload)
                details["verification_feedback_inbox"] = inbox[-40:]
            node.details = details
            node.node_version += 1
            source_reference = str(details.get("latest_supervisor_review_path") or "")
            source_path = self.artifacts.resolve(source_reference) if source_reference else Path()
            communication_ids: list[str] = []
            if source_path.is_file():
                for communication in decision.communications:
                    stored = repo.add_architecture_communication(
                        run,
                        node,
                        attention=communication.attention,
                        title=communication.title,
                        question=communication.question,
                        global_consequence=communication.global_consequence,
                        affected_node_ids=list(communication.affected_node_ids),
                        related_evidence_ids=list(communication.related_evidence_ids),
                        local_pressure_tests=communication.local_pressure_tests,
                        rationale=communication.rationale,
                        source_path=source_path,
                    )
                    communication_ids.append(stored.communication_id)
            # Local completion/candidate formation is not automatically progress on
            # the original Contract.  Only an explicitly controlling Claim identity
            # or a later graph-level decision resets root stagnation.
            repo.add_global_state_delta(
                run,
                node=node,
                change_kind="supervisor_review",
                old_summary=old_summary,
                new_summary={
                    "status": decision.node_status,
                    "diagnosis": decision.diagnosis,
                    "finding_ids": finding_ids,
                    "candidate_artifact_id": candidate_result[0] if candidate_result else None,
                    "architecture_attention": decision.architecture_attention,
                    "communication_ids": communication_ids,
                },
                evidence_refs=[*finding_ids, *([candidate_result[0]] if candidate_result else [])],
                gap_key=gap_key,
                root_relevant=root_relevant,
            )
            internal_event = repo.append_event("node", node_id, "supervisor_review", decision.diagnosis, {
                "node_status": decision.node_status, "major_event": decision.major_event,
                "assignment_count": len(decision.assignments), "finding_count": len(decision.findings),
            })
            public_source_key = internal_event.event_id
            finding_lines = "\n".join(
                f"- {item.title} · {item.finding_type} · {item.significance}"
                for item in decision.findings
            ) or "无。"
            repo.request_public_summary(
                public_source_key,
                run_id=run_id,
                mode="event",
                title="局部研究组形成阶段判断",
                content=(
                    f"Node title: {node.title}\n"
                    f"Current diagnosis:\n{decision.diagnosis}\n\n"
                    f"Node status: {decision.node_status}\n"
                    f"Claim candidate formed: {'yes' if decision.claim_candidate else 'no'}\n"
                    f"Research findings:\n{finding_lines}"
                ),
                source_role="node_supervisor",
                node_id=node_id,
                source_created_at=internal_event.created_at.isoformat(),
            )
        if public_source_key:
            self._start_public_summary_task(public_source_key)
        self._refresh_architecture_snapshot(run_id, root_progress=root_relevant)
        self._maybe_schedule_architecture_review(run_id)
        return candidate_result

    @staticmethod
    def _suppress_new_assignments(decision: SupervisorDecision) -> SupervisorDecision:
        """Preserve findings/candidates while enforcing the local-review fuse.

        The final permitted Supervisor review may still contain valuable mathematics,
        but its proposed next tasks must not be materialized.  This prevents the exact
        failure mode where the outer review count is exhausted while a fresh batch of
        assignments silently enters the inner worker queue.
        """
        return SupervisorDecision(
            diagnosis=decision.diagnosis,
            assignments=[],
            retire_worker_labels=decision.retire_worker_labels,
            findings=decision.findings,
            node_status=decision.node_status,
            major_event=decision.major_event,
            claim_candidate=decision.claim_candidate,
            supervisor_notes=decision.supervisor_notes,
            raw_markdown=decision.raw_markdown,
            communications=decision.communications,
            architecture_attention=decision.architecture_attention,
            completion_assessment=decision.completion_assessment,
        )

    # ------------------------------------------------------------------ verification
    def _candidate_metadata(
        self, node_id: str, candidate_id: str, candidate_path: Path
    ) -> dict[str, Any]:
        """读取不可变 Candidate 的 canonical statement、proof 与 completion scope。"""
        candidate_text = candidate_path.read_text(encoding="utf-8")
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                raise DynamicProverError("Candidate 所属 Node 不存在")
            stored = dict(
                (dict(node.details or {}).get("candidate_artifacts") or {}).get(
                    candidate_id
                ) or {}
            )
            node_kind = node.kind
            controlling_id = self._controlling_proposition_id(node_id)
        if stored.get("canonical_statement") and stored.get("proof_sha256"):
            return stored
        # Historical candidates predate explicit scope.  They may still be verified
        # as auxiliary facts, but can never complete a Claim Node.
        legacy_text = candidate_text
        if not re.search(r"^#{2,3}\s+(?:Candidate Scope|候选范围)\s*$", legacy_text, re.MULTILINE):
            legacy_text += "\n\n### Candidate Scope\n\nauxiliary_fact\n"
        identity, validation = parse_candidate_identity(
            legacy_text,
            node_kind=node_kind,
            controlling_proposition_id=controlling_id,
        )
        if not validation.ok or identity is None:
            def section(*headings: str) -> str:
                matches = list(re.finditer(
                    r"^#{2,3}\s+(.+?)\s*$", candidate_text, flags=re.MULTILINE
                ))
                accepted = {heading.casefold() for heading in headings}
                for index, match in enumerate(matches):
                    if match.group(1).strip().casefold() not in accepted:
                        continue
                    start = match.end()
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(candidate_text)
                    return candidate_text[start:end].strip()
                return ""
            statement = section("精确陈述", "Exact Statement")
            proof = section("完整论证", "Complete Argument")
            return {
                "path": self.artifacts.reference(candidate_path),
                "sha256": sha256_file(candidate_path),
                "canonical_statement": statement,
                "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                "proof_sha256": hashlib.sha256(proof.encode("utf-8")).hexdigest(),
                "candidate_scope": "auxiliary_fact",
                "controlling_proposition_id": None,
                "validation_errors": list(validation.errors),
            }
        return {
            "path": self.artifacts.reference(candidate_path),
            "sha256": sha256_file(candidate_path),
            "canonical_statement": identity.statement,
            "statement_sha256": hashlib.sha256(
                identity.statement.encode("utf-8")
            ).hexdigest(),
            "proof_sha256": hashlib.sha256(identity.proof.encode("utf-8")).hexdigest(),
            "candidate_scope": identity.scope,
            "controlling_proposition_id": identity.controlling_proposition_id or None,
        }

    def _existing_verified_decision(
        self,
        run_id: str,
        node_id: str,
        candidate_id: str,
        candidate_sha256: str,
        statement_sha256: str,
        canonical_statement: str,
    ) -> VerificationDecision | None:
        """读取同一 Candidate 或同 Node 同一精确命题的既有 PASS。"""
        with self.database.session() as session:
            claim = RepositoryHub(session, self.artifacts).find_verified_claim_for_candidate(
                run_id,
                node_id,
                candidate_artifact_id=candidate_id,
                candidate_sha256=candidate_sha256,
                statement_sha256=statement_sha256,
            )
            if claim is None:
                return None
            verified_result = claim.statement
            report_ref = claim.verifier_report_path
        if report_ref:
            report_path = self.artifacts.resolve(report_ref)
            if report_path.is_file():
                markdown = report_path.read_text(encoding="utf-8")
                decision, validation = parse_verification(markdown)
                if validation.ok and decision is not None and decision.verdict == "PASS":
                    return VerificationDecision(
                        "PASS",
                        decision.statement,
                        decision.decisive_issues,
                        decision.raw_markdown,
                        claim.reuse_boundary or decision.reuse_boundary,
                        claim.statement or decision.verified_result,
                    )
        markdown = (
            "# Fresh Claim Verification\n\n"
            "## Verdict\n\nPASS\n\n"
            f"## Exact Statement\n\n{canonical_statement}\n\n"
            f"## Verified Mathematical Result\n\n{verified_result}\n\n"
            "## Assumption and Dependency Check\n\n"
            "该 Candidate 已有持久化 fresh PASS；本次未重复调用模型。\n\n"
            "## Decisive Mathematical Analysis\n\n"
            "复用已有核验结论。\n\n"
            "## Reuse Boundary\n\n仅在同一不可变 Candidate Artifact 下复用。\n"
        )
        return VerificationDecision(
            "PASS", canonical_statement, "复用已有核验结论。", markdown,
            claim.reuse_boundary if claim is not None else "",
            verified_result,
        )

    async def _verify_candidate_path(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        node_id: str,
        candidate_id: str,
        candidate_path: Path,
    ) -> tuple[VerificationDecision, bool]:
        """核验一个不可变 Candidate，并只在首次 PASS 时登记公开数学结果。"""
        candidate = candidate_path.read_text(encoding="utf-8")
        metadata = self._candidate_metadata(node_id, candidate_id, candidate_path)
        canonical_statement = str(metadata["canonical_statement"]).strip()
        statement_sha256 = str(metadata.get("statement_sha256") or "")
        candidate_sha256 = sha256_file(candidate_path)
        existing = self._existing_verified_decision(
            run_id, node_id, candidate_id, candidate_sha256, statement_sha256,
            canonical_statement,
        )
        if existing is not None:
            return existing, False
        report_path = self._node_root(run_id, node_id) / "verification" / f"{candidate_id}.md"
        validation_errors = [str(item) for item in metadata.get("validation_errors") or []]
        if validation_errors:
            statement = canonical_statement or "Candidate 未提供可核验的 canonical exact statement。"
            markdown = (
                "# Fresh Claim Verification\n\n"
                "## Verdict\n\nREJECT\n\n"
                f"## Exact Statement\n\n{statement}\n\n"
                "## Verified Mathematical Result\n\n不适用。\n\n"
                "## Assumption and Dependency Check\n\n"
                "Candidate 未通过 Controller 的 cold-start 自包含门禁。\n\n"
                "## Decisive Mathematical Analysis\n\n"
                + "\n".join(f"- {item}" for item in validation_errors)
                + "\n\n## Reuse Boundary\n\n不适用。\n"
            )
            self.artifacts.write_text(
                report_path.relative_to(self.artifacts.data_root), markdown
            )
            decision = VerificationDecision(
                "REJECT", statement, "\n".join(validation_errors), markdown, "不适用。"
            )
            self._record_verification_feedback(
                run_id, node_id, candidate_id, verdict="REJECT", markdown=markdown
            )
            return decision, False
        with self.database.session() as session:
            allowed_claims = self._claims_markdown(
                RepositoryHub(session, self.artifacts).list_verified_claims(run_id)
            )
        task = f"""# Fresh Claim Verification

你没有当前研究组聊天或私有笔记。只检查下面自包含 candidate 是否严格证明或反驳其精确 statement。

Candidate Artifact ID: {candidate_id}

## Canonical Exact Statement

{canonical_statement}

## Candidate

{candidate}

## Allowed Verified Claims

{allowed_claims or '无。'}

上面列出的 Claims 已完成 Fresh Verification，可以作为黑盒使用，不要重新证明；
但必须逐项检查 Candidate 是否满足其假设、参数范围与 Reuse Boundary，并排除循环依赖。
未列出的 Claim、相似但不相同的结论和未核验研究材料不得作为已证事实使用。

不要修补证明，不要把“方向正确”当作 PASS。
"""
        def verifier_parser(text: str) -> tuple[VerificationDecision | None, ValidationResult]:
            return parse_verification(text)

        decision, _ = await self._validated_role(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=node_id,
            role="claim_verifier", role_key=f"claim_verifier:{candidate_id}",
            scope_type="verification", scope_id=candidate_id, task=task,
            parser=verifier_parser, persistent=False,
            operation_kind="claim_verification",
            checkpoint_payload={
                "node_id": node_id,
                "candidate_id": candidate_id,
                "candidate_path": self.artifacts.reference(candidate_path),
            },
        )
        self.artifacts.write_text(report_path.relative_to(self.artifacts.data_root), decision.raw_markdown)
        if decision.verdict == "PASS":
            public_source_key: str | None = None
            try:
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    existing_claim = repo.find_verified_claim_for_candidate(
                        run_id,
                        node_id,
                        candidate_artifact_id=candidate_id,
                        candidate_sha256=candidate_sha256,
                        statement_sha256=statement_sha256,
                    )
                    if existing_claim is not None:
                        return decision, False
                    node = session.get(ResearchNodeRecord, node_id)
                    run = session.get(ResearchRunRecord, run_id)
                    if node is None or run is None:
                        raise DynamicProverError("PASS 入库时 Node/Run 不存在")
                    claim = repo.add_verified_claim(
                        run_id,
                        node_id,
                        verdict="PASS",
                        statement=decision.verified_result,
                        candidate_path=candidate_path,
                        verifier_report_path=report_path,
                        candidate_artifact_id=candidate_id,
                        statement_sha256=str(metadata.get("statement_sha256") or ""),
                        proof_sha256=str(metadata.get("proof_sha256") or ""),
                        candidate_scope=str(metadata.get("candidate_scope") or "auxiliary_fact"),
                        controlling_proposition_id=(
                            str(metadata.get("controlling_proposition_id"))
                            if metadata.get("controlling_proposition_id") else None
                        ),
                        reuse_boundary=decision.reuse_boundary,
                        node_version=node.node_version,
                        graph_version=run.graph_version,
                        verification_policy_version=VERIFICATION_POLICY_VERSION,
                    )
                    certificate_path = (
                        self._run_root(run_id)
                        / "verified"
                        / claim.verified_claim_id
                        / "verification_certificate.json"
                    )
                    self.artifacts.write_json(
                        certificate_path.relative_to(self.artifacts.data_root),
                        {
                            "schema_version": 1,
                            "verified_claim_id": claim.verified_claim_id,
                            "candidate_artifact_id": candidate_id,
                            "canonical_statement": canonical_statement,
                            "verified_mathematical_result": decision.verified_result,
                            "statement_sha256": claim.statement_sha256,
                            "proof_sha256": claim.proof_sha256,
                            "candidate_sha256": claim.candidate_sha256,
                            "verifier_verdict": "PASS",
                            "verifier_report_sha256": claim.verifier_report_sha256,
                            "reuse_boundary": decision.reuse_boundary,
                            "candidate_scope": claim.candidate_scope,
                            "controlling_proposition_id": claim.controlling_proposition_id,
                            "node_id": node_id,
                            "node_version": node.node_version,
                            "graph_version": run.graph_version,
                            "verification_policy_version": VERIFICATION_POLICY_VERSION,
                            "verified_at": claim.created_at.isoformat(),
                        },
                    )
                    claim.certificate_path = self.artifacts.reference(certificate_path)
                    repo.append_event(
                        "run",
                        run_id,
                        "verified_claim",
                        f"Node {node_id} Claim 已通过 fresh verification",
                        {"node_id": node_id, "candidate_artifact_id": candidate_id},
                    )
                    public_source_key = claim.verified_claim_id
                    repo.request_public_summary(
                        public_source_key,
                        run_id=run_id,
                        mode="verified",
                        title="新结果通过独立核验",
                        content=(
                            f"Verdict: {decision.verdict}\n"
                            f"Verified mathematical result:\n{decision.verified_result}\n\n"
                            f"Reuse boundary:\n{decision.reuse_boundary}\n\n"
                            f"Decisive boundary:\n{decision.decisive_issues}"
                        ),
                        node_id=node_id,
                        source_created_at=claim.created_at.isoformat(),
                    )
            except IntegrityError:
                existing = self._existing_verified_decision(
                    run_id,
                    node_id,
                    candidate_id,
                    candidate_sha256,
                    statement_sha256,
                    canonical_statement,
                )
                if existing is None:
                    raise
                return existing, False
            if public_source_key:
                self._start_public_summary_task(public_source_key)
            return decision, True
        self._record_verification_feedback(
            run_id,
            node_id,
            candidate_id,
            verdict="REJECT",
            markdown=decision.raw_markdown,
        )
        return decision, False

    async def _complete_node_after_verification(
        self, run_id: str, node_id: str, candidate_id: str
    ) -> bool:
        """仅让 Claim Node 因 fresh PASS 自动完成；Discovery 只登记事实并继续承担原责任。"""
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                raise DynamicProverError("Verified Claim 所属 Node 不存在")
            node_kind = node.kind
            node_status = node.status
            candidate = dict(
                (dict(node.details or {}).get("candidate_artifacts") or {}).get(candidate_id)
                or {}
            )
            claim = session.scalar(select(VerifiedClaimRecord).where(
                VerifiedClaimRecord.run_id == run_id,
                VerifiedClaimRecord.node_id == node_id,
                VerifiedClaimRecord.candidate_artifact_id == candidate_id,
                VerifiedClaimRecord.verdict == "PASS",
            ).limit(1))
        if node_kind != "claim":
            await self._emit_stage(
                run_id,
                {
                    "node_id": node_id,
                    "kind": "verified_claim",
                    "major": True,
                    "node_status": node_status,
                    "discovery_continues": True,
                },
            )
            return False
        controlling_id = self._controlling_proposition_id(node_id)
        candidate_scope = str(
            candidate.get("candidate_scope")
            or (claim.candidate_scope if claim is not None else "")
        )
        proposition_id = str(
            candidate.get("controlling_proposition_id")
            or (claim.controlling_proposition_id if claim is not None else "")
        )
        if (
            candidate_scope != "controlling_node_claim"
            or proposition_id != controlling_id
        ):
            await self._emit_stage(
                run_id,
                {
                    "node_id": node_id,
                    "kind": "verified_claim",
                    "major": True,
                    "node_status": node_status,
                    "claim_continues": True,
                    "candidate_scope": "auxiliary_fact",
                    "root_relevant": False,
                },
            )
            return False
        if node_status == "completed":
            return True
        await self._cancel_node_workers(node_id)
        _, pending_report_ids = self._pending_worker_report_inbox(node_id)
        if pending_report_ids:
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                repo.append_event(
                    "node",
                    node_id,
                    "verified_claim_completion_deferred",
                    "Fresh Verification 已通过；仍有迟到 Worker report，必须先由 Supervisor 消费",
                    {
                        "candidate_artifact_id": candidate_id,
                        "pending_report_ids": pending_report_ids,
                    },
                )
            return False
        self._freeze_verified_claim_final_stage(run_id, node_id, candidate_id)
        self._set_node_status(node_id, "completed")
        await self._emit_stage(
            run_id,
            {
                "node_id": node_id,
                "kind": "verified_claim",
                "major": True,
                "node_status": "completed",
                "candidate_artifact_id": candidate_id,
                "root_relevant": True,
            },
        )
        return True

    async def _review_candidate_verification(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        node_id: str,
        candidate: tuple[str, Path],
        *,
        major: bool,
    ) -> tuple[bool, str, dict[str, Any]]:
        """安全核验 Supervisor 候选，并给全局 Reviewer 保留完整可读证据。"""
        candidate_id, candidate_path = candidate
        verdict, verification_error, newly_verified = await self._verify_candidate_safely(
            user_id, run_id, attempt_id, node_id, candidate_id, candidate_path
        )
        if verdict is not None and verdict.verdict == "PASS":
            node_kind = self._get_node(node_id).kind
            completed = (
                await self._complete_node_after_verification(
                    run_id, node_id, candidate_id
                )
                if newly_verified or node_kind == "claim"
                else False
            )
            if completed:
                return True, "", {}
            if not newly_verified:
                return False, verdict.raw_markdown, {
                    "node_id": node_id,
                    "kind": "local_review",
                    "major": False,
                    "node_status": self._get_node(node_id).status,
                }
            return False, verdict.raw_markdown, {
                "node_id": node_id,
                "kind": "verified_claim",
                "major": True,
                "candidate_artifact_id": candidate_id,
                "verification_verdict": "PASS",
                "discovery_continues": True,
            }
        if verdict is None:
            feedback = f"Fresh verification 暂不可用：{verification_error}"
            return False, feedback, {
                "node_id": node_id,
                "kind": "claim_verification_unavailable",
                "major": major,
                "candidate_artifact_id": candidate_id,
                "error": verification_error,
            }
        report_path = self._node_root(run_id, node_id) / "verification" / f"{candidate_id}.md"
        return False, verdict.raw_markdown, {
            "node_id": node_id,
            "kind": "claim_candidate_rejected",
            "major": major,
            "candidate_artifact_id": candidate_id,
            "verification_verdict": verdict.verdict,
            "verifier_report_path": self.artifacts.reference(report_path),
            "verifier_report_sha256": sha256_file(report_path),
            "verifier_report_markdown": verdict.raw_markdown,
        }

    # ------------------------------------------------------------------ architecture review
    async def _emit_stage(self, run_id: str, event: dict[str, Any]) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None or run.status != "running":
                return
            node = session.get(ResearchNodeRecord, str(event.get("node_id") or ""))
            references = [
                str(value) for key, value in event.items()
                if key.endswith("_id") or key.endswith("_path")
                if value
            ]
            kind = str(event.get("kind") or "node_state_changed")
            root_relevant = bool(event.get("root_relevant")) or kind == "graph_mutated"
            if kind != "local_review":
                repo.add_global_state_delta(
                    run,
                    node=node,
                    change_kind=kind,
                    new_summary=dict(event),
                    evidence_refs=references,
                    root_relevant=root_relevant,
                )
        self._refresh_architecture_snapshot(run_id, root_progress=root_relevant)
        self._maybe_schedule_architecture_review(run_id)

    def _architecture_trigger_reasons(self, run_id: str) -> list[str]:
        """按显式 Inbox、周期 checkpoint、shared gap、stagnation 与 closure 计算触发原因。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None or run.status != "running":
                return []
            repo.mark_stale_architecture_communications(run_id)
            communications = repo.pending_architecture_communications(run_id)
            deltas = repo.pending_global_state_deltas(run_id)
            snapshot = session.get(ArchitectureSnapshotRecord, run_id)
            nodes = repo.list_nodes(run_id)
            reasons: list[str] = []
            if any(item.attention == "urgent" for item in communications):
                reasons.append("urgent_communication")
            next_review = [item for item in communications if item.attention == "next_review"]
            if len(next_review) >= self.settings.architecture_review_communication_threshold:
                reasons.append("communication_batch")
            if len({item.node_id for item in communications}) >= self.settings.architecture_review_distinct_node_threshold:
                reasons.append("cross_node_questions")
            gap_nodes: dict[str, set[str]] = {}
            for item in deltas:
                if item.gap_key and item.node_id:
                    gap_nodes.setdefault(item.gap_key, set()).add(item.node_id)
            if any(
                len(node_ids) >= self.settings.architecture_shared_bottleneck_node_threshold
                for node_ids in gap_nodes.values()
            ):
                reasons.append("shared_bottleneck")
            started = run.started_at or run.created_at
            elapsed = self._run_research_elapsed_seconds(run_id, started)
            last_review = float(snapshot.last_review_research_seconds if snapshot else 0)
            if elapsed - last_review >= self.settings.architecture_review_interval_seconds:
                reasons.append("periodic_checkpoint")
            last_progress_seconds = float(
                (snapshot.payload or {}).get("last_root_progress_research_seconds", 0)
                if snapshot else 0
            )
            if elapsed - last_progress_seconds >= self.settings.architecture_root_stagnation_seconds:
                reasons.append("root_stagnation")
            live = [node for node in nodes if node.status in {"ready", "active", "draining"}]
            if nodes and not live and (deltas or communications):
                reasons.append("closure_checkpoint")
            return list(dict.fromkeys(reasons))

    def _queue_closure_checkpoint(self, run_id: str) -> bool:
        """所有 Node 终态时持久请求 closure review，禁止退化为 no-runnable 自动暂停。"""
        closure_state = self._post_drain_closure_state(run_id)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None or run.status != "running":
                return False
            nodes = repo.list_nodes(run_id)
            if not nodes or any(
                node.status in {"ready", "active", "draining"} for node in nodes
            ):
                return False
            if repo.pending_global_state_deltas(run_id) or repo.pending_architecture_communications(run_id):
                return False
            claims = repo.list_verified_claims(run_id)
            final_claim_ids = [
                claim.verified_claim_id
                for claim in claims
                if (
                    (node := session.get(ResearchNodeRecord, claim.node_id)) is not None
                    and bool((node.details or {}).get("is_final_claim"))
                )
            ]
            retry_index = 1 + sum(
                1 for event in repo.read_events("run", run_id)
                if event.kind == "run_closure_pending"
            )
            summary = {
                "terminal_node_ids": [node.node_id for node in nodes],
                "verified_claim_ids": [claim.verified_claim_id for claim in claims],
                "final_claim_ids": final_claim_ids,
                "required_action": (
                    "Use a verified Final Claim for COMPLETE_RUN, or SPAWN a Final Claim Node "
                    "that assembles every remaining Contract branch."
                ),
                "retry_index": retry_index,
                "closure_source": (
                    str(closure_state.get("source") or "") if closure_state else "natural"
                ),
                "preferred_completion_kind": (
                    str(closure_state.get("preferred_completion_kind") or "")
                    if closure_state else "fully_resolved"
                ),
            }
            repo.add_global_state_delta(
                run,
                change_kind="closure_pending",
                new_summary=summary,
                evidence_refs=[claim.verified_claim_id for claim in claims],
                gap_key="run_closure",
                root_relevant=True,
            )
            repo.append_event(
                "run",
                run_id,
                "run_closure_pending",
                "全部研究节点已收束，正在核验最终 Claim 与论文写作交接",
                summary,
            )
            return True

    def _is_post_drain_final_claim(self, run_id: str, node: Any) -> bool:
        """用现有 Final Claim 标记与 closure 事件识别轻量组装节点。"""
        return bool(
            node.kind == "claim"
            and (node.details or {}).get("is_final_claim")
            and self._post_drain_closure_state(run_id) is not None
        )

    def _final_local_stages_markdown(
        self, run_id: str, *, exclude_node_id: str
    ) -> str:
        """为 post-drain Final Claim Supervisor 汇集已经冻结的局部最终交付。"""
        parts: list[str] = []
        with self.database.session() as session:
            nodes = RepositoryHub(session, self.artifacts).list_nodes(run_id)
            entries = [
                (
                    node.node_id,
                    node.title,
                    node.status,
                    node.final_stage_path,
                )
                for node in nodes
                if node.node_id != exclude_node_id and node.final_stage_path
            ]
        for node_id, title, status, reference in entries:
            parts.append(
                f"### {node_id} · {title}\n"
                f"Status: {status}\n\n"
                f"{self._read_public_artifact(reference)}"
            )
        return "\n\n".join(parts)

    def _post_drain_closure_state(self, run_id: str) -> dict[str, Any] | None:
        """读取最近一次分层 drain 后、尚未完成出版的持久 closure handoff。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            events = repo.read_events("run", run_id)
            last_reopened = max(
                (item.sequence for item in events if item.kind == "research_reopened"),
                default=0,
            )
            completed_after_reopen = any(
                item.kind == "run_completed" and item.sequence > last_reopened
                for item in events
            )
            if completed_after_reopen:
                return None
            event = next((
                item for item in reversed(events)
                if item.kind == "closure_drain_completed"
                and item.sequence > last_reopened
            ), None)
            return dict(event.payload or {}) if event is not None else None

    def _enter_post_drain_closure(
        self,
        run_id: str,
        attempt_id: str,
        *,
        source: str,
        preferred_completion_kind: str,
    ) -> None:
        """Worker/Supervisor 全部收束后恢复 Reviewer 阶段，不直接启动 Writer。"""
        self._run_stop_events.setdefault(run_id, asyncio.Event()).clear()
        self._completion_requests.pop(run_id, None)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            run.status = "running"
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt is not None:
                attempt.status = "running"
            existing = next((
                item for item in reversed(repo.read_events("run", run_id))
                if item.kind == "closure_drain_completed"
            ), None)
            if existing is None:
                repo.append_event(
                    "run",
                    run_id,
                    "closure_drain_completed",
                    "Worker 与 Supervisor 已完成分层收束，等待最终全局 Reviewer",
                    {
                        "attempt_id": attempt_id,
                        "source": source,
                        "preferred_completion_kind": preferred_completion_kind,
                    },
                )
        self._refresh_architecture_snapshot(run_id, root_progress=True)
        self._queue_closure_checkpoint(run_id)
        self._maybe_schedule_architecture_review(run_id, force=True)

    def _maybe_schedule_architecture_review(self, run_id: str, *, force: bool = False) -> None:
        """低成本轮询全局 checkpoint；运行中的 Reviewer 只留下后续信号。"""
        now = time.monotonic()
        if now < self._architecture_failure_until.get(run_id, 0.0):
            return
        if not force and now - self._architecture_poll_at.get(run_id, 0.0) < 1.0:
            return
        self._architecture_poll_at[run_id] = now
        if self._architecture_trigger_reasons(run_id):
            self._ensure_architecture_task(run_id)

    def _ensure_architecture_task(self, run_id: str) -> None:
        task = self._architecture_tasks.get(run_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._architecture_review_loop(run_id), name=f"mathlab-architecture-{run_id}")
        self._architecture_tasks[run_id] = task
        task.add_done_callback(lambda finished, rid=run_id: self._architecture_task_done(rid, finished))

    def _architecture_task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        # Consume exceptions so a failed review cannot become an unobserved task error.
        failed = False
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - exercised by integration failure paths
            failed = True
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                repo.append_event("run", run_id, "architecture_review_failed", str(exc))
        finally:
            if self._architecture_tasks.get(run_id) is task:
                self._architecture_tasks.pop(run_id, None)
        if failed:
            self._architecture_failure_until[run_id] = time.monotonic() + 60.0
        else:
            self._architecture_failure_until.pop(run_id, None)
        try:
            asyncio.get_running_loop().call_soon(self._maybe_schedule_architecture_review, run_id)
        except RuntimeError:
            pass

    async def _architecture_review_loop(self, run_id: str) -> None:
        lock = self._architecture_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            while True:
                trigger_reasons = self._architecture_trigger_reasons(run_id)
                if not trigger_reasons:
                    return
                with self.database.session() as session:
                    run = session.get(ResearchRunRecord, run_id)
                    paused_operation = session.scalar(select(AgentOperationCheckpointRecord).where(
                        AgentOperationCheckpointRecord.run_id == run_id,
                        AgentOperationCheckpointRecord.operation_kind == "architecture_review",
                        AgentOperationCheckpointRecord.status == "paused",
                        AgentOperationCheckpointRecord.pause_epoch == (run.pause_epoch if run is not None else -1),
                    ).order_by(AgentOperationCheckpointRecord.created_at.desc()).limit(1))
                    paused_payload = dict(paused_operation.payload or {}) if paused_operation else {}
                    paused_role_key = str(paused_operation.role_key) if paused_operation else ""
                review_id = str(paused_payload.get("review_id") or new_id("review"))
                if paused_payload.get("trigger_reasons"):
                    trigger_reasons = list(paused_payload["trigger_reasons"])
                snapshot = self._architecture_snapshot(run_id, review_id)
                closure_state = self._post_drain_closure_state(run_id)
                task = f"""# Fresh Research Architecture Review

## Research Contract

{snapshot['contract']}

## Current Architecture Snapshot

{snapshot['snapshot_markdown']}

## Architecture Decision Ledger

{snapshot['ledger_markdown']}

## Global State Delta

{snapshot['delta_markdown']}

## Pending Architecture Communications

{snapshot['communication_markdown']}

## Promotion Proposals

{snapshot['promotions']}

## Verified Claims

{snapshot['verified_claims']}

## Artifact Index

{snapshot['artifact_index_markdown']}

## Resource Boundary

Active Nodes: {snapshot['active_count']}
Ready Nodes: {snapshot['ready_count']}
Max Parallel Nodes: {self.settings.max_parallel_research_nodes}
Promotion Slots This Review: {snapshot['promotion_slots']}

Trigger Reasons: {', '.join(trigger_reasons)}

## Post-drain Closure Handoff

{json.dumps(closure_state or {}, ensure_ascii=False, indent=2)}

若该对象非空，Worker 与 Node Supervisor 已经完成最后交付，本轮是 Writer 前的最终全局审查：

- `budget_exhausted` / `user_graceful_drain` 不再开启新研究 Node；核对 Final Stages 后用准确的 Completion Kind 请求 `COMPLETE_RUN`；
- natural / reviewer closure 若只有普通 Verified Claim 而没有 Verified Final Claim，先 SPAWN 一个 `Final Claim: yes` 的轻量 Claim Node，使用现有 Final Stages 与 Verified Claims 组装 Target-level statement；不要重新开展底层探索；
- `Evidence ID` 只能填写一个完整 Claim ID，其他 artifact 写入 Evidence Inspected 或 Reason。

只使用七个允许 Action。Promotion slots 是上限不是配额。Structured/Directional 方向成熟时，用 SPAWN 写出完整 Node Card；不要使用 ACTIVATE。Reviewer 不直接创建 Worker。Active Node 无 Action 也会继续。

`## Promotion Decisions` 对每个 pending proposal 使用以下自然结构：

### <proposal_id>
Decision: accept / return_local / defer / reject / merge
Reason: [数学与机会成本理由]

需要核实局部材料时，使用 Controller 提供的 reviewer_evidence list/open 工具；在 `Evidence Inspected On Demand` 中列出实际打开的 artifact ID。不要把 private notes 当数学证据。
"""
                expected_promotions = dict(snapshot["promotion_findings"])

                def review_parser(text: str) -> tuple[ArchitectureDecision | None, ValidationResult]:
                    parsed, validation = parse_architecture_review(text)
                    if not validation.ok or parsed is None:
                        return parsed, validation
                    errors = list(validation.errors)
                    submitted = {item.proposal_id for item in parsed.promotion_decisions}
                    missing = sorted(set(expected_promotions) - submitted)
                    unknown = sorted(submitted - set(expected_promotions))
                    if missing:
                        errors.append("Promotion Decisions 缺少：" + "、".join(missing))
                    if unknown:
                        errors.append("Promotion Decisions 包含非本轮 proposal：" + "、".join(unknown))
                    action_findings = {item.finding_id for item in parsed.actions if item.finding_id}
                    for item in parsed.promotion_decisions:
                        if item.decision in {"accept", "merge"} and expected_promotions.get(item.proposal_id) not in action_findings:
                            errors.append(f"Promotion `{item.proposal_id}` 决定 {item.decision} 时必须有绑定 Finding ID 的 SPAWN/MERGE Action")
                    return parsed, ValidationResult(errors, list(validation.warnings))

                def evidence_validator(
                    parsed: ArchitectureDecision | None,
                    agent_turn: AgentTurn,
                    raw_text: str,
                ) -> ValidationResult:
                    inspected = (
                        parsed.evidence_inspected
                        if parsed is not None
                        else markdown_section(raw_text, "Evidence Inspected On Demand")
                    )
                    claimed = set(re.findall(r"\bartifact-\d{4}\b", inspected))
                    opened = set(str(item) for item in dict(agent_turn.evidence or {}).get("opened_artifact_ids", []))
                    errors = []
                    if claimed - opened:
                        errors.append("Evidence Inspected On Demand 声明了未通过 Controller open 的 artifact：" + "、".join(sorted(claimed - opened)))
                    if inspected.strip().lower() not in {"", "无", "无。", "暂无", "暂无。", "none"} and not opened:
                        errors.append("Evidence Inspected On Demand 非空，但本轮没有真实 reviewer_evidence open 记录")
                    return ValidationResult(errors, [])

                decision, turn = await self._validated_role(
                    user_id=snapshot["user_id"], run_id=run_id, attempt_id=snapshot["attempt_id"], node_id=None,
                    role="architecture_reviewer", role_key=paused_role_key or f"architecture_reviewer:{review_id}",
                    scope_type="architecture_review", scope_id=review_id, task=task,
                    parser=review_parser, persistent=False,
                    reviewer_evidence_artifacts=snapshot["evidence_artifacts"],
                    turn_validator=evidence_validator,
                    operation_kind="architecture_review",
                    checkpoint_payload={
                        "review_id": review_id,
                        "trigger_reasons": trigger_reasons,
                        "graph_version": snapshot["graph_version"],
                        "snapshot_revision": snapshot["snapshot_revision"],
                        "node_preconditions": snapshot["node_preconditions"],
                        "delta_ids": snapshot["delta_ids"],
                        "communication_ids": snapshot["communication_ids"],
                        "promotion_slots": snapshot["promotion_slots"],
                        "review_sequence": snapshot["review_sequence"],
                    },
                )
                path = self._run_root(run_id) / "runtime" / "architecture" / f"review-{snapshot['review_sequence']:03d}.md"
                self.artifacts.write_text(path.relative_to(self.artifacts.data_root), decision.raw_markdown)
                before = snapshot["graph_version"]
                preconditions_current = self._architecture_review_preconditions_current(
                    run_id,
                    snapshot_revision=snapshot["snapshot_revision"],
                    graph_version=before,
                    node_preconditions=snapshot["node_preconditions"],
                )
                applied = await self._apply_architecture_decision(
                    run_id,
                    decision,
                    snapshot["promotion_slots"],
                    review_id=review_id,
                    expected_graph_version=before,
                    global_preconditions_current=preconditions_current,
                    review_node_preconditions=snapshot["node_preconditions"],
                )
                actions_body = markdown_section(decision.raw_markdown, "Actions").strip().lower()
                explicitly_no_actions = actions_body in {
                    "", "无", "无。", "暂无", "暂无。", "none", "n/a"
                }
                if (
                    closure_state
                    and preconditions_current
                    and not decision.actions
                    and explicitly_no_actions
                ):
                    completion_kind = str(
                        closure_state.get("preferred_completion_kind")
                        or "partially_resolved"
                    )
                    self._completion_requests[run_id] = completion_kind
                    self._run_stop_events.setdefault(run_id, asyncio.Event()).set()
                    applied.append({
                        "type": "COMPLETE_RUN",
                        "target": "run",
                        "reason": (
                            "Post-drain Reviewer inspected every Final Stage and raised no "
                            "new research action."
                        ),
                        "completion_kind": completion_kind,
                        "evidence_id": None,
                        "applied_as": "COMPLETE_RUN_REQUESTED_POST_DRAIN_DEFAULT",
                    })
                    with self.database.session() as session:
                        RepositoryHub(session, self.artifacts).append_event(
                            "run",
                            run_id,
                            "run_closure_committed",
                            "最终全局 Reviewer 未提出新的研究动作，正在交接论文写作",
                            {
                                "completion_kind": completion_kind,
                                "source": closure_state.get("source"),
                                "review_id": review_id,
                            },
                        )
                review_applied = preconditions_current or any(
                    item.get("applied_as") in {
                        "CONTINUE_REACTIVATED",
                        "CONTINUE_DIRECTIVE_RECORDED",
                    }
                    for item in applied
                )
                public_source_key: str | None = None
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    run = session.get(ResearchRunRecord, run_id)
                    if run is None:
                        return
                    repo.add_architecture_review(
                        run, trigger="+".join(trigger_reasons),
                        promotion_slots=snapshot["promotion_slots"], decision_path=path,
                        actions=applied, graph_version_before=before,
                        graph_version_after=run.graph_version,
                        review_id=review_id,
                        provider_thread_id=turn.thread_id,
                        trigger_reasons=trigger_reasons,
                        input_snapshot_revision=snapshot["snapshot_revision"],
                        input_node_preconditions=snapshot["node_preconditions"],
                        application_status=("applied" if review_applied else "stale"),
                        input_delta_ids=snapshot["delta_ids"],
                        input_communication_ids=snapshot["communication_ids"],
                        evidence_index_path=snapshot["artifact_index_path"],
                        evidence_inspected=dict(turn.evidence or {}),
                        snapshot_update_markdown=decision.snapshot_update,
                        ledger_updates_markdown=decision.ledger_updates,
                    )
                    if preconditions_current:
                        self._apply_promotion_decisions(repo, decision)
                        self._append_ledger_updates(repo, run, review_id, decision.ledger_updates)
                        repo.consume_architecture_inputs(
                            review_id,
                            delta_ids=snapshot["delta_ids"],
                            communication_ids=snapshot["communication_ids"],
                        )
                    internal_event = repo.append_event(
                        "run", run_id,
                        "architecture_review" if review_applied else "architecture_review_stale",
                        decision.diagnosis,
                        {"actions": applied, "application_status": (
                            "applied" if review_applied else "stale"
                        )},
                    )
                    if review_applied:
                        public_source_key = internal_event.event_id
                        repo.request_public_summary(
                            public_source_key,
                            run_id=run_id,
                            mode="event",
                            title="全局研究复盘",
                            content=(
                                f"Global diagnosis:\n{decision.diagnosis}\n\n"
                                "Applied architecture actions:\n"
                                + json.dumps(applied, ensure_ascii=False, indent=2)
                            ),
                            source_role="architecture_reviewer",
                            source_created_at=internal_event.created_at.isoformat(),
                        )
                if public_source_key:
                    self._start_public_summary_task(public_source_key)
                with self.database.session() as session:
                    run = session.get(ResearchRunRecord, run_id)
                    started = (run.started_at or run.created_at) if run is not None else utc_now()
                self._refresh_architecture_snapshot(
                    run_id,
                    reviewer_patch=(decision.snapshot_update if preconditions_current else None),
                    last_review_research_seconds=(
                        self._run_research_elapsed_seconds(run_id, started)
                        if preconditions_current else None
                    ),
                )
                # 运行期间到达的 urgent/Delta 保持 pending；下一次循环会使用 fresh thread。

    def _architecture_review_preconditions_current(
        self,
        run_id: str,
        *,
        snapshot_revision: int,
        graph_version: int,
        node_preconditions: dict[str, dict[str, Any]],
    ) -> bool:
        """用 snapshot revision 与逐 Node CAS 拒绝在过期白板上形成的 Action。"""
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            snapshot = session.get(ArchitectureSnapshotRecord, run_id)
            if (
                run is None
                or snapshot is None
                or run.graph_version != graph_version
                or snapshot.revision != snapshot_revision
            ):
                return False
            current = {
                node.node_id: {
                    "node_version": node.node_version,
                    "status": node.status,
                }
                for node in session.scalars(select(ResearchNodeRecord).where(
                    ResearchNodeRecord.run_id == run_id
                ))
            }
            return current == node_preconditions

    def _reactivate_node_from_review(
        self,
        session: Any,
        node: ResearchNodeRecord,
        *,
        review_id: str | None,
        reason: str,
        directive: str,
    ) -> bool:
        """按精确 Reviewer 指令恢复 blocked、技术失败或过早完成的 Discovery，并复用原团队。"""
        recoverable = node.status in {"blocked", "failed"} or (
            node.status == "completed" and node.kind == "discovery"
        )
        if not recoverable or not directive:
            return False
        details = dict(node.details or {})
        history = list(details.get("reactivation_stage_history") or [])
        if node.final_stage_path:
            history.append({
                "path": node.final_stage_path,
                "sha256": node.final_stage_sha256,
                "node_version": node.node_version,
                "status": node.status,
            })
        details["reactivation_stage_history"] = history
        details["resume_count"] = int(details.get("resume_count") or 0) + 1
        details["resumed_by_review_id"] = review_id
        details["resume_reason"] = reason
        details["architecture_directives"] = (
            list(details.get("architecture_directives") or []) + [directive]
        )[-20:]
        details.pop("drain_target_status", None)
        details.pop("drain_reason", None)
        node.details = details
        node.final_stage_path = None
        node.final_stage_sha256 = None
        node.status = "ready"
        node.node_version += 1
        now = utc_now()
        for worker in session.scalars(select(WorkerIdentityRecord).where(
            WorkerIdentityRecord.node_id == node.node_id
        )):
            if worker.status == "retired" or worker.retired_at is not None:
                continue
            worker.status = "idle"
            worker.updated_at = now
        return True

    async def _apply_architecture_decision(
        self,
        run_id: str,
        decision: ArchitectureDecision,
        promotion_slots: int,
        *,
        review_id: str | None = None,
        expected_graph_version: int | None = None,
        global_preconditions_current: bool = True,
        review_node_preconditions: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        closure_state = self._post_drain_closure_state(run_id)
        spawn_count = 0
        deferred_async: list[tuple[str, Any]] = []
        scheduled_verifications: set[tuple[str, str]] = set()
        public_node_ids: list[str] = []
        resumed_node_ids: list[str] = []
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            if (
                global_preconditions_current
                and expected_graph_version is not None
                and run.graph_version != expected_graph_version
            ):
                return [
                    {
                        "type": action.action_type,
                        "target": action.target,
                        "reason": action.reason,
                        "applied_as": "REJECTED_STALE_GRAPH_VERSION",
                    }
                    for action in decision.actions
                ]
            if run.status not in {"created", "queued", "running"}:
                return [
                    {
                        "type": action.action_type,
                        "target": action.target,
                        "reason": action.reason,
                        "applied_as": "IGNORED_RUN_DRAINING",
                    }
                    for action in decision.actions
                ]
            verified = {item.verified_claim_id: item for item in repo.list_verified_claims(run_id)}
            for action in decision.actions:
                record = {
                    "type": action.action_type,
                    "target": action.target,
                    "reason": action.reason,
                    "evidence_id": action.evidence_id or None,
                    "completion_kind": action.completion_kind or None,
                }
                if not global_preconditions_current:
                    targeted_continue_current = (
                        action.action_type == "CONTINUE"
                        and action.target not in {"all", "run"}
                        and self._targeted_continue_precondition_current(
                            session,
                            run_id,
                            action.target,
                            review_node_preconditions or {},
                        )
                    )
                    if not targeted_continue_current:
                        record["applied_as"] = "STALE_REVIEW_RETRY_REQUIRED"
                        applied.append(record)
                        continue
                if action.action_type == "CONTINUE":
                    broad_target = action.target in {"all", "run"}
                    targets = repo.list_nodes(run_id) if broad_target else [session.get(ResearchNodeRecord, action.target)]
                    valid_targets = [item for item in targets if item is not None and item.run_id == run_id]
                    reactivated = []
                    for node in valid_targets:
                        if not broad_target and self._reactivate_node_from_review(
                            session,
                            node,
                            review_id=review_id,
                            reason=action.reason,
                            directive=action.supervisor_directive,
                        ):
                            resumed_node_ids.append(node.node_id)
                            reactivated.append(node.node_id)
                            continue
                        if node.status in {"ready", "active", "draining"}:
                            details = dict(node.details or {})
                            directives = list(details.get("architecture_directives") or [])
                            if action.supervisor_directive:
                                directives.append(action.supervisor_directive)
                            details["architecture_directives"] = directives[-20:]
                            node.details = details
                    if reactivated:
                        record["applied_as"] = "CONTINUE_REACTIVATED"
                        record["reactivated_node_ids"] = reactivated
                    elif any(item.status in {"ready", "active", "draining"} for item in valid_targets):
                        record["applied_as"] = "CONTINUE_DIRECTIVE_RECORDED"
                    elif valid_targets:
                        record["applied_as"] = "IGNORED_TERMINAL_TARGET"
                    else:
                        record["applied_as"] = "IGNORED_INVALID_TARGET"
                elif action.action_type in {"SPAWN_DISCOVERY_NODE", "SPAWN_CLAIM_NODE"}:
                    if spawn_count >= promotion_slots:
                        record["applied_as"] = "DEFERRED_NO_PROMOTION_SLOT"
                    else:
                        kind = "discovery" if action.action_type == "SPAWN_DISCOVERY_NODE" else "claim"
                        parent_record = session.get(ResearchNodeRecord, action.target)
                        parent = action.target if parent_record is not None and parent_record.run_id == run_id else None
                        if action.target not in {"run", "all"} and parent is None:
                            record["applied_as"] = "REJECTED_INVALID_TARGET"
                            applied.append(record)
                            continue
                        finding = session.get(ResearchFindingRecord, action.finding_id) if action.finding_id else None
                        finding_node = session.get(ResearchNodeRecord, finding.node_id) if finding else None
                        if action.finding_id and (finding is None or finding_node is None or finding_node.run_id != run_id):
                            record["applied_as"] = "REJECTED_INVALID_FINDING"
                            applied.append(record)
                            continue
                        details = {
                            "origin_direction": action.origin_direction,
                            "activation_evidence": action.activation_evidence,
                            "spawn_reason": action.reason,
                            "is_final_claim": bool(kind == "claim" and action.is_final_claim),
                            "original_contract_uncertainty_reduced": action.original_contract_uncertainty_reduced,
                        }
                        node = repo.create_node(
                            run, kind=kind, title=action.title, objective=action.objective,
                            completion_criterion=action.completion_criterion, details=details,
                            parent_node_id=parent, origin_finding_id=action.finding_id or None,
                            origin_review_id=review_id, status="ready",
                        )
                        if kind == "claim":
                            node_details = dict(node.details or {})
                            node_details["controlling_proposition_id"] = self._controlling_proposition_id(node.node_id)
                            node.details = node_details
                        if parent and action.relation:
                            repo.create_edge(run_id, parent, node.node_id, action.relation, action.condition)
                        if finding is not None:
                            finding.status = "promoted"
                            if finding.proposal:
                                finding.proposal.status = "accepted"
                        run.graph_version += 1
                        spawn_count += 1
                        public_node_ids.append(node.node_id)
                        record.update({"applied_as": action.action_type, "spawned_node_id": node.node_id})
                elif action.action_type == "RETIRE":
                    node = session.get(ResearchNodeRecord, action.target)
                    if node is None or node.run_id != run_id or node.status in TERMINAL_NODE_STATES:
                        record["applied_as"] = "IGNORED_INVALID_TARGET"
                    elif action.evidence_id and action.evidence_id not in verified:
                        record["applied_as"] = "REJECTED_INVALID_EVIDENCE"
                    else:
                        details = dict(node.details or {})
                        details["drain_target_status"] = "retired"
                        details["retire_reason"] = action.reason
                        details["retire_evidence_id"] = action.evidence_id
                        node.details = details
                        node.status = "draining"
                        self._node_draining.setdefault(node.node_id, asyncio.Event()).set()
                        record["applied_as"] = "RETIRE_DRAINING"
                elif action.action_type == "REQUEST_FRESH_VERIFICATION":
                    node = session.get(ResearchNodeRecord, action.target)
                    candidate = ((node.details or {}).get("candidate_artifacts") or {}).get(action.candidate_artifact_id) if node else None
                    if node is None or node.run_id != run_id or not candidate:
                        record["applied_as"] = "REJECTED_MISSING_CANDIDATE_ARTIFACT"
                    else:
                        candidate_path = self.artifacts.resolve(candidate["path"])
                        candidate_sha256 = (
                            sha256_file(candidate_path) if candidate_path.is_file() else ""
                        )
                        verification_key = (node.node_id, action.candidate_artifact_id)
                        existing_claim = repo.find_verified_claim_for_candidate(
                            run_id,
                            node.node_id,
                            candidate_artifact_id=action.candidate_artifact_id,
                            candidate_sha256=candidate_sha256,
                        )
                        if existing_claim is not None:
                            record["applied_as"] = "ALREADY_VERIFIED"
                            record["verified_claim_id"] = existing_claim.verified_claim_id
                        elif verification_key in scheduled_verifications:
                            record["applied_as"] = "VERIFICATION_ALREADY_IN_FLIGHT"
                        elif repo.active_candidate_verification(
                            run_id, action.candidate_artifact_id
                        ) is not None:
                            record["applied_as"] = "VERIFICATION_ALREADY_IN_FLIGHT"
                        else:
                            latest_attempt = max(run.attempts, key=lambda item: item.attempt_number)
                            scheduled_verifications.add(verification_key)
                            deferred_async.append(("verify", (
                                run.owner_user_id,
                                latest_attempt.attempt_id,
                                node.node_id,
                                action.candidate_artifact_id,
                                candidate_path,
                                record,
                            )))
                            record["applied_as"] = "VERIFICATION_REQUESTED"
                elif action.action_type == "MERGE_NODES":
                    if spawn_count >= promotion_slots:
                        record["applied_as"] = "DEFERRED_NO_PROMOTION_SLOT"
                    else:
                        unique_source_ids = list(dict.fromkeys(action.source_node_ids))
                        source_nodes = [session.get(ResearchNodeRecord, item) for item in unique_source_ids]
                        valid_sources = [item for item in source_nodes if item and item.run_id == run_id]
                        if len(valid_sources) < 2:
                            record["applied_as"] = "REJECTED_INVALID_MERGE_SOURCES"
                        else:
                            for node in valid_sources:
                                if node.status in TERMINAL_NODE_STATES and node.final_stage_path:
                                    continue
                                if node.status not in TERMINAL_NODE_STATES:
                                    details = dict(node.details or {})
                                    details["drain_target_status"] = "retired"
                                    details["merge_reason"] = action.reason
                                    node.details = details
                                    node.status = "draining"
                                    self._node_draining.setdefault(node.node_id, asyncio.Event()).set()
                            latest_attempt = max(run.attempts, key=lambda item: item.attempt_number)
                            deferred_async.append(("merge", (run.owner_user_id, latest_attempt.attempt_id, action, review_id)))
                            spawn_count += 1
                            record["applied_as"] = "MERGE_DRAINING"
                elif action.action_type == "COMPLETE_RUN":
                    nodes = repo.list_nodes(run_id)
                    findings = list(session.scalars(
                        select(ResearchFindingRecord)
                        .join(ResearchNodeRecord, ResearchFindingRecord.node_id == ResearchNodeRecord.node_id)
                        .where(ResearchNodeRecord.run_id == run_id)
                    ))
                    allowed = False
                    gate_failures: list[str] = []
                    if action.completion_kind == "partially_resolved":
                        allowed = bool(verified) or bool(
                            closure_state
                            and closure_state.get("source") == "user_graceful_drain"
                        )
                        if not allowed:
                            gate_failures.append("partial_completion_requires_verified_mathematics")
                    elif action.completion_kind == "structurally_blocked":
                        allowed = bool(
                            any(node.status == "blocked" for node in nodes)
                            or any(item.finding_type == "structural_obstruction" for item in findings)
                        )
                        if not allowed:
                            gate_failures.append("structurally_blocked_requires_blocked_node_or_obstruction")
                    elif action.completion_kind == "budget_exhausted":
                        elapsed = self._run_research_elapsed_seconds(
                            run_id, run.started_at or run.created_at
                        )
                        allowed = elapsed >= self._run_duration_seconds(run)
                        if not allowed:
                            gate_failures.append("budget_not_exhausted")
                    if action.completion_kind == "fully_resolved":
                        controlling = verified.get(action.evidence_id)
                        controlling_node = session.get(ResearchNodeRecord, controlling.node_id) if controlling else None
                        if controlling is None:
                            gate_failures.append("controlling_evidence_id_not_found")
                        elif controlling.verdict != "PASS":
                            gate_failures.append("controlling_evidence_not_pass")
                        if controlling_node is None:
                            gate_failures.append("controlling_node_not_found")
                        elif not bool((controlling_node.details or {}).get("is_final_claim")):
                            gate_failures.append("controlling_node_is_not_final_claim")
                        if controlling is not None and controlling.candidate_scope != "controlling_node_claim":
                            gate_failures.append("evidence_is_not_controlling_claim")
                        if (
                            controlling is not None
                            and controlling_node is not None
                            and controlling.controlling_proposition_id
                            != self._controlling_proposition_id(controlling_node.node_id)
                        ):
                            gate_failures.append("controlling_proposition_id_mismatch")
                        allowed = not gate_failures
                    if allowed:
                        self._completion_requests[run_id] = action.completion_kind
                        self._run_stop_events.setdefault(run_id, asyncio.Event()).set()
                        record["applied_as"] = "COMPLETE_RUN_REQUESTED"
                        repo.append_event(
                            "run",
                            run_id,
                            "run_closure_committed",
                            "最终 Claim 与完成类型已通过门禁，正在交接论文写作",
                            {
                                "completion_kind": action.completion_kind,
                                "evidence_id": action.evidence_id,
                                "review_id": review_id,
                            },
                        )
                    else:
                        record["applied_as"] = "REJECTED_COMPLETION_GATE"
                        record["gate_failures"] = gate_failures
                        repo.add_global_state_delta(
                            run,
                            change_kind="completion_gate_rejected",
                            new_summary={
                                "review_id": review_id,
                                "completion_kind": action.completion_kind,
                                "evidence_id": action.evidence_id,
                                "gate_failures": gate_failures,
                                "required_action": (
                                    "Correct the completion evidence field. For fully_resolved, "
                                    "SPAWN and verify a Final Claim Node if the cited Claim is not final."
                                ),
                            },
                            evidence_refs=[action.evidence_id] if action.evidence_id else [],
                            gap_key="run_closure",
                            root_relevant=True,
                        )
                        repo.append_event(
                            "run",
                            run_id,
                            "completion_gate_rejected",
                            "完成请求未通过最终 Claim 门禁，已返回全局 Reviewer 修复",
                            {
                                "completion_kind": action.completion_kind,
                                "evidence_id": action.evidence_id,
                                "gate_failures": gate_failures,
                                "review_id": review_id,
                            },
                        )
                applied.append(record)
        for node_id in public_node_ids:
            self._queue_node_public_card(node_id)
        for node_id in resumed_node_ids:
            self._node_draining[node_id] = asyncio.Event()
        for kind, payload in deferred_async:
            if kind == "verify":
                user_id, attempt_id, node_id, candidate_id, path, action_record = payload
                verdict, verification_error, newly_verified = await self._verify_candidate_safely(
                    user_id, run_id, attempt_id, node_id, candidate_id, path
                )
                if verdict is None:
                    action_record["applied_as"] = "VERIFICATION_UNAVAILABLE"
                    action_record["error"] = verification_error
                elif verdict.verdict == "PASS":
                    if newly_verified:
                        completed = await self._complete_node_after_verification(
                            run_id, node_id, candidate_id
                        )
                        if not completed:
                            action_record["applied_as"] = "VERIFIED_AUXILIARY_FACT"
                    else:
                        action_record["applied_as"] = "ALREADY_VERIFIED"
                else:
                    action_record["applied_as"] = "VERIFICATION_REJECTED"
                    action_record["verification_verdict"] = "REJECT"
                    action_record["feedback_delivered_to_node"] = True
            elif kind == "merge":
                user_id, attempt_id, action, origin_review_id = payload
                await self._finish_merge(user_id, run_id, attempt_id, action, origin_review_id=origin_review_id)
        effective = [item for item in applied if str(item.get("applied_as") or "").startswith((
            "SPAWN_", "MERGE_", "RETIRE_", "CONTINUE_REACTIVATED", "COMPLETE_RUN_",
        ))]
        root_progress = any(
            action.action_type == "COMPLETE_RUN"
            or bool(action.original_contract_uncertainty_reduced.strip())
            for action in decision.actions
        )
        if effective:
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                run = session.get(ResearchRunRecord, run_id)
                if run is not None:
                    repo.add_global_state_delta(
                        run,
                        change_kind="architecture_actions_applied",
                        new_summary={"review_id": review_id, "actions": effective},
                        evidence_refs=[review_id] if review_id else [],
                        root_relevant=root_progress,
                    )
            self._refresh_architecture_snapshot(run_id, root_progress=root_progress)
        return applied

    @staticmethod
    def _targeted_continue_precondition_current(
        session: Any,
        run_id: str,
        node_id: str,
        node_preconditions: dict[str, dict[str, Any]],
    ) -> bool:
        """只比较 CONTINUE 目标 Node，忽略无关 Node 的并发推进。"""
        expected = node_preconditions.get(node_id)
        node = session.get(ResearchNodeRecord, node_id)
        if expected is None or node is None or node.run_id != run_id:
            return False
        if {
            "node_version": node.node_version,
            "status": node.status,
        } != expected:
            return False
        return node.status in {"ready", "active", "draining", "blocked", "failed"} or (
            node.status == "completed" and node.kind == "discovery"
        )

    async def _finish_merge(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        action: Any,
        *,
        origin_review_id: str | None = None,
    ) -> None:
        source_ids = list(action.source_node_ids)
        tasks = [self._node_tasks.get(run_id, {}).get(node_id) for node_id in source_ids]
        tasks = [task for task in tasks if task and not task.done()]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self.settings.node_drain_hard_seconds)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        merged_node_id: str | None = None
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            stages = []
            for node_id in source_ids:
                node = session.get(ResearchNodeRecord, node_id)
                if node and node.final_stage_path:
                    stages.append({"node_id": node_id, "path": node.final_stage_path, "sha256": node.final_stage_sha256})
            kind = action.node_kind if action.node_kind in {"discovery", "claim"} else "discovery"
            merged = repo.create_node(
                run, kind=kind, title=action.title or "Merged Research Node",
                objective=action.objective or action.reason,
                completion_criterion=action.completion_criterion or "形成明确研究结论并满足 fresh verification / discovery completion discipline。",
                details={
                    "merged_from": source_ids,
                    "source_final_stages": stages,
                    "merge_reason": action.reason,
                    "original_contract_uncertainty_reduced": action.original_contract_uncertainty_reduced,
                },
                origin_review_id=origin_review_id,
                status="ready",
            )
            if kind == "claim":
                merged_details = dict(merged.details or {})
                merged_details["controlling_proposition_id"] = self._controlling_proposition_id(merged.node_id)
                merged.details = merged_details
            merged_node_id = merged.node_id
            for node_id in source_ids:
                node = session.get(ResearchNodeRecord, node_id)
                if node:
                    node.status = "retired"
                    node.merged_into_node_id = merged.node_id
            run.graph_version += 1
            repo.append_event("run", run_id, "nodes_merged", f"{source_ids} merged into {merged.node_id}", {"merged_node_id": merged.node_id})
        if merged_node_id:
            self._queue_node_public_card(merged_node_id)

    # ------------------------------------------------------------------ draining / finalization
    def _request_node_drain(self, node_id: str, target_status: str, reason: str) -> None:
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node and node.status not in TERMINAL_NODE_STATES:
                details = dict(node.details or {})
                details["drain_target_status"] = target_status
                details["drain_reason"] = reason
                node.details = details
                node.status = "draining"
        self._node_draining.setdefault(node_id, asyncio.Event()).set()

    async def _drain_node(self, user_id: str, run_id: str, attempt_id: str, node_id: str, target_status: str) -> None:
        # No new assignments are launched once status is draining.
        with self.database.session() as session:
            for assignment in session.scalars(select(WorkerAssignmentRecord).where(
                WorkerAssignmentRecord.node_id == node_id,
                WorkerAssignmentRecord.status == "ready",
            )):
                assignment.status = "cancelled"
                assignment.completed_at = utc_now()
                worker = session.get(WorkerIdentityRecord, assignment.worker_id)
                if worker is not None and worker.status == "assigned":
                    worker.status = "idle"
        tasks = [task for task in self._worker_tasks.get(node_id, {}).values() if not task.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(
                        float(self.settings.node_drain_soft_seconds),
                        float(self.settings.node_drain_hard_seconds) / 2.0,
                    ),
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        await self._final_supervisor_stage(user_id, run_id, attempt_id, node_id)
        self._set_node_status(node_id, target_status)
        await self._emit_stage(
            run_id,
            {
                "node_id": node_id,
                "kind": f"node_{target_status}",
                "major": target_status == "blocked",
            },
        )

    async def _final_supervisor_stage(self, user_id: str, run_id: str, attempt_id: str, node_id: str) -> None:
        stage_version = self._get_node(node_id).node_version
        reports, report_ids = self._pending_worker_report_inbox(node_id)
        durable_feedback, feedback_ids = self._pending_verification_feedback(node_id)
        if not report_ids and not feedback_ids:
            node = self._get_node(node_id)
            path = self._write_latest_supervisor_final_stage(
                run_id,
                node,
                boundary=(
                    "Research drain reached after all formal Worker reports and "
                    "Fresh Verifier feedback had already been consumed."
                ),
            )
            if path is not None:
                with self.database.session() as session:
                    stored = session.get(ResearchNodeRecord, node_id)
                    if stored is not None:
                        stored.final_stage_path = self.artifacts.reference(path)
                        stored.final_stage_sha256 = sha256_file(path)
                self._refresh_architecture_snapshot(run_id)
                return
        decision = await self._supervisor_review(
            user_id, run_id, attempt_id, node_id, bootstrap=False, reports=reports,
            verification_feedback="\n\n".join(filter(None, [
                "上层已要求 draining：禁止新任务，只提交 Final Local Stage、Findings 与必要 Promotion。",
                durable_feedback,
            ])),
        )
        # Never materialize assignments while draining. Preserve findings/notes/candidate only.
        safe = SupervisorDecision(
            diagnosis=decision.diagnosis, assignments=[], retire_worker_labels=[],
            findings=decision.findings, node_status="进入收束", major_event=decision.major_event,
            claim_candidate=decision.claim_candidate, supervisor_notes=decision.supervisor_notes,
            raw_markdown=decision.raw_markdown,
            communications=decision.communications,
            architecture_attention=decision.architecture_attention,
            completion_assessment=decision.completion_assessment,
        )
        self._apply_supervisor_decision(
            user_id, run_id, node_id, safe, bootstrap=False,
            consumed_report_ids=report_ids,
            consumed_feedback_ids=feedback_ids,
            counts_toward_fuse=False,
        )
        path = self._node_root(run_id, node_id) / f"final-local-stage-v{stage_version:03d}.md"
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), decision.raw_markdown)
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node:
                node.final_stage_path = self.artifacts.reference(path)
                node.final_stage_sha256 = sha256_file(path)
        self._refresh_architecture_snapshot(run_id)

    def _write_latest_supervisor_final_stage(
        self,
        run_id: str,
        node: Any,
        *,
        boundary: str,
    ) -> Path | None:
        """在没有未消费输入时，把最近一次完整 Supervisor Review 确定性冻结为 Final Stage。"""
        details = dict(node.details or {})
        review_reference = str(details.get("latest_supervisor_review_path") or "")
        if not review_reference:
            return None
        review_path = self.artifacts.resolve(review_reference)
        if not review_path.is_file():
            return None
        expected_sha = str(details.get("latest_supervisor_review_sha256") or "")
        if expected_sha and sha256_file(review_path) != expected_sha:
            raise DynamicProverError(
                f"最新 Supervisor Review SHA 漂移：{node.node_id}"
            )
        review = review_path.read_text(encoding="utf-8")
        path = self._node_root(run_id, node.node_id) / f"final-local-stage-v{node.node_version:03d}.md"
        text = "\n\n".join([
            "# Final Local Stage",
            f"## Node Responsibility\n\n{node.objective}",
            f"## Latest Complete Supervisor Review\n\n{review}",
            f"## Research Boundary\n\n{boundary}",
        ]) + "\n"
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), text)
        return path

    def _freeze_final_stage_from_decision(
        self,
        run_id: str,
        node_id: str,
        decision: SupervisorDecision,
    ) -> Path:
        """直接冻结已完成 Discovery/blocked 的同一份 Local Review，避免额外模型调用。"""
        node = self._get_node(node_id)
        path = self._node_root(run_id, node_id) / f"final-local-stage-v{node.node_version:03d}.md"
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), decision.raw_markdown)
        with self.database.session() as session:
            stored = session.get(ResearchNodeRecord, node_id)
            if stored is not None:
                stored.final_stage_path = self.artifacts.reference(path)
                stored.final_stage_sha256 = sha256_file(path)
        self._refresh_architecture_snapshot(run_id, root_progress=False)
        return path

    def _freeze_verified_claim_final_stage(
        self, run_id: str, node_id: str, candidate_id: str
    ) -> Path:
        """用公开 Supervisor/Candidate/Verifier 证据确定性组装 Claim PASS 的 Final Local Stage。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                raise DynamicProverError("Claim Node 不存在")
            claim = session.scalar(select(VerifiedClaimRecord).where(
                VerifiedClaimRecord.run_id == run_id,
                VerifiedClaimRecord.node_id == node_id,
                VerifiedClaimRecord.verdict == "PASS",
                VerifiedClaimRecord.candidate_artifact_id == candidate_id,
            ).order_by(VerifiedClaimRecord.created_at.desc()).limit(1))
            details = dict(node.details or {})
            supervisor_reference = str(details.get("latest_supervisor_review_path") or "")
            supervisor_text = self._read_public_artifact(supervisor_reference) if supervisor_reference else ""
            candidate_text = self._read_public_artifact(claim.candidate_path) if claim else ""
            verifier_text = self._read_public_artifact(claim.verifier_report_path) if claim else ""
            text = "\n\n".join([
                "# Final Local Stage",
                f"## Node Responsibility\n\n{node.objective}",
                "## Last Supervisor Review\n\n" + (supervisor_text or "无。"),
                "## Verified Candidate\n\n" + (candidate_text or "无。"),
                "## Fresh Verifier Verdict\n\n" + (verifier_text or "无。"),
                "## Completion Boundary\n\nClaim Node 仅因上述 controlling Fresh Verifier PASS 而完成。",
            ]) + "\n"
            path = self._node_root(run_id, node_id) / f"final-local-stage-v{node.node_version:03d}.md"
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), text)
            node.final_stage_path = self.artifacts.reference(path)
            node.final_stage_sha256 = sha256_file(path)
            if claim is not None:
                repo.add_global_state_delta(
                    session.get(ResearchRunRecord, run_id),
                    node=node,
                    change_kind="claim_verified_completed",
                    new_summary={"verified_claim_id": claim.verified_claim_id, "status": "completed"},
                    evidence_refs=[claim.verified_claim_id],
                    root_relevant=True,
                )
        self._refresh_architecture_snapshot(run_id, root_progress=True)
        return path

    async def _drain_run(self, run_id: str, reason: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run:
                run.status = "draining"
            for node in repo.list_nodes(run_id):
                if node.status in {"ready", "active", "draining"}:
                    details = dict(node.details or {})
                    details["drain_target_status"] = "retired" if node.status != "ready" else "retired"
                    details["drain_reason"] = reason
                    node.details = details
                    node.status = "draining"
                    self._node_draining.setdefault(node.node_id, asyncio.Event()).set()
            repo.append_event("run", run_id, "run_draining", reason)
        architecture = self._architecture_tasks.get(run_id)
        if architecture is not None and not architecture.done():
            architecture.cancel()
            await asyncio.gather(architecture, return_exceptions=True)
        tasks = [task for task in self._node_tasks.get(run_id, {}).values() if not task.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.settings.node_drain_hard_seconds,
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._close_drained_agent_operations(run_id)

    def _close_drained_agent_operations(self, run_id: str) -> None:
        """Research drain 返回前关闭已无真实进程的研究 operation checkpoint。"""
        with self.database.session() as session:
            now = utc_now()
            for operation in session.scalars(
                select(AgentOperationCheckpointRecord).where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.status.in_([
                        "running", "paused", "capacity_wait",
                    ]),
                )
            ):
                operation.status = "cancelled"
                operation.error = None
                operation.updated_at = now
                operation.completed_at = now

    async def _cancel_node_workers(self, node_id: str) -> None:
        tasks = [task for task in self._worker_tasks.get(node_id, {}).values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            run = session.get(ResearchRunRecord, node.run_id) if node is not None else None
            if run is not None and run.status != "paused":
                for operation in session.scalars(select(AgentOperationCheckpointRecord).where(
                    AgentOperationCheckpointRecord.node_id == node_id,
                    AgentOperationCheckpointRecord.operation_kind == "worker_assignment",
                    AgentOperationCheckpointRecord.status.in_(["running", "paused", "capacity_wait"]),
                )):
                    operation.status = "cancelled"
                    operation.error = "Node lifecycle cancelled the unfinished Worker operation"
                    operation.updated_at = utc_now()
                    operation.completed_at = utc_now()

    def _authorize_publication(
        self,
        run_id: str,
        attempt_id: str,
        completion_kind: str,
        *,
        source: str,
    ) -> None:
        """持久记录唯一合法的 Writer 启动授权，禁止以“没有活跃 Node”替代终局决定。"""
        allowed_sources = {"budget_exhausted", "user_graceful_drain", "reviewer_complete_run"}
        if source not in allowed_sources:
            raise DynamicProverError(f"非法 Publication Authorization 来源：{source}")
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            try:
                _, created = repo.authorize_publication(
                    run_id, attempt_id, completion_kind, source
                )
            except ValueError as exc:
                raise DynamicProverError(str(exc)) from exc
            if not created:
                return
            repo.append_event(
                "run",
                run_id,
                "publication_authorized",
                "Research Run 已通过论文写作启动门禁",
                {
                    "attempt_id": attempt_id,
                    "completion_kind": completion_kind,
                    "source": source,
                },
            )

    def _require_publication_authorization(
        self, run_id: str, attempt_id: str, completion_kind: str
    ) -> None:
        """在 Writer 前重新读取持久状态，拒绝缺失、过期或与当前 Attempt 不一致的授权。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            state = repo.publication_state(run_id, attempt_id)
            if (
                state is None
                or state.completion_kind != completion_kind
                or state.status not in {"authorized", "publishing", "published"}
            ):
                raise DynamicProverError(
                    "Writer 启动被拒绝：缺少 Publication Authorization / 当前 Attempt Publication State"
                )
            events = repo.read_events("run", run_id)
            last_reopened = max(
                (item.sequence for item in events if item.kind == "research_reopened"),
                default=0,
            )
            authorization = next((
                item for item in reversed(events)
                if item.kind == "publication_authorized" and item.sequence > last_reopened
            ), None)
            if authorization is None:
                raise DynamicProverError("Writer 启动被拒绝：缺少 Publication Authorization")
            payload = dict(authorization.payload or {})
            if payload.get("attempt_id") != attempt_id or payload.get("completion_kind") != completion_kind:
                raise DynamicProverError("Writer 启动被拒绝：Publication Authorization 与当前 Attempt 不一致")

    async def _finalize_run(
        self, user_id: str, run_id: str, attempt_id: str, *, completion_kind: str
    ) -> None:
        """原子 claim 出版权；并发恢复路径只有一个能进入昂贵 Writer 链。"""
        self._require_publication_authorization(run_id, attempt_id, completion_kind)
        self._require_publication_integrity(run_id)
        with self.database.session() as session:
            claimed = RepositoryHub(session, self.artifacts).claim_publication(
                run_id, attempt_id, claimed_by=self.settings.runner_id
            )
        if not claimed:
            return
        try:
            await self._finalize_run_claimed(
                user_id, run_id, attempt_id, completion_kind=completion_kind
            )
        except Exception as exc:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).finish_publication(
                    run_id, attempt_id, status="failed", error=str(exc)[:4000]
                )
            raise
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).finish_publication(
                run_id, attempt_id, status="published"
            )

    def _require_publication_integrity(self, run_id: str) -> None:
        """Writer 前只拒绝真实数据损坏；内部收束完整度不再是出版门禁。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Writer 启动被拒绝：Research Run 不存在")
            contract_path = self.artifacts.resolve(run.contract_snapshot_path)
            if not contract_path.is_file():
                raise DynamicProverError("Writer 启动被拒绝：Contract snapshot 不存在")
            for node in repo.list_nodes(run_id):
                if not node.final_stage_path:
                    continue
                stage_path = self.artifacts.resolve(node.final_stage_path)
                if not stage_path.is_file() or "degraded" in stage_path.name.lower():
                    continue
                if node.final_stage_sha256 and sha256_file(stage_path) != node.final_stage_sha256:
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Final Local Stage SHA-256 不一致：{node.node_id}"
                    )
            for claim in repo.list_verified_claims(run_id):
                if claim.verdict.upper() != "PASS":
                    continue
                if not claim.statement.strip():
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim statement 为空：{claim.verified_claim_id}"
                    )
                if not claim.candidate_path:
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim Candidate 未登记：{claim.verified_claim_id}"
                    )
                candidate_path = self.artifacts.resolve(claim.candidate_path)
                if not candidate_path.is_file():
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim Candidate 不存在：{claim.verified_claim_id}"
                    )
                if claim.sha256 and sha256_file(candidate_path) != claim.sha256:
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim Candidate SHA-256 不一致：{claim.verified_claim_id}"
                    )
                if not claim.verifier_report_path:
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim Verifier report 未登记：{claim.verified_claim_id}"
                    )
                verifier_path = self.artifacts.resolve(claim.verifier_report_path)
                if not verifier_path.is_file():
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verified Claim Verifier report 不存在：{claim.verified_claim_id}"
                    )
                if (
                    claim.verifier_report_sha256
                    and sha256_file(verifier_path) != claim.verifier_report_sha256
                ):
                    raise DynamicProverError(
                        f"Writer 启动被拒绝：Verifier report SHA-256 不一致：{claim.verified_claim_id}"
                    )

    async def _finalize_run_claimed(
        self, user_id: str, run_id: str, attempt_id: str, *, completion_kind: str
    ) -> None:
        """在已取得 Publication State single-flight 后执行封口、写作与发布。"""
        await self._honor_pause_barrier(run_id)
        await self._reconcile_run_final_state(run_id)
        await self._honor_pause_barrier(run_id)
        report_path = await self._write_final_report(user_id, run_id, attempt_id, completion_kind)
        await self._wait_public_summaries(run_id)
        source_path = report_path.with_suffix(".tex")
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            run.status = "completed"
            run.completion_kind = completion_kind
            run.completed_at = utc_now()
            run.terminal_at = run.completed_at
            run.final_paper_pdf_path = self.artifacts.reference(report_path)
            run.final_paper_tex_path = self.artifacts.reference(source_path)
            run.pre_pause_status = None
            run.pre_pause_phase = None
            run.pause_reason = None
            run.pause_requested_at = None
            run.pause_requested_by = None
            run.paused_at = None
            run.resume_requested_at = None
            run.claimed_by_runner = None
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt:
                attempt.status = "completed"
                attempt.completed_at = utc_now()
            run.conversation.status = "completed"
            run.conversation.completed_run_id = run_id
            actual = repo.settle_run_cost(run_id)
            repo.append_event("run", run_id, "run_completed", f"Research Run 完成：{completion_kind}", {"actual_cost": actual})
        self.final_snapshots.export_run(run_id)

    async def _honor_pause_barrier(self, run_id: str) -> None:
        """在收束与出版边界阻止 paused/429 Run 偷跑到 Writer。"""
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            requested = run.pause_requested_at is not None
            status = run.status
            reason = run.pause_reason or "user"
        if requested and status != "paused":
            await self._enter_run_pause(run_id, reason=reason)
            status = "paused"
        if status in {"paused", "resuming"}:
            await self._wait_until_resumed(run_id)

    async def _reconcile_run_final_state(self, run_id: str) -> None:
        """在 Writer 取证前停止研究调度并冻结当前真实存在的数学材料。"""
        await self._cancel_run_children(run_id)
        self._architecture_poll_at.pop(run_id, None)
        now = utc_now()
        summary = {
            "nodes_reconciled": 0,
            "nodes_without_final_stage": 0,
            "pending_reports": 0,
            "pending_feedback": 0,
            "assignments_cancelled": 0,
            "assignments_interrupted": 0,
            "workers_retired": 0,
        }
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            for node in repo.list_nodes(run_id):
                pending_reports = int(session.scalar(
                    select(func.count())
                    .select_from(WorkerReportRecord)
                    .join(WorkerAssignmentRecord)
                    .where(
                        WorkerAssignmentRecord.node_id == node.node_id,
                        WorkerReportRecord.consumption_status != "consumed",
                    )
                ) or 0)
                pending_feedback = sum(
                    1
                    for item in list(dict(node.details or {}).get(
                        "verification_feedback_inbox"
                    ) or [])
                    if str(dict(item).get("status") or "pending") == "pending"
                )
                summary["pending_reports"] += pending_reports
                summary["pending_feedback"] += pending_feedback
                if not node.final_stage_path:
                    summary["nodes_without_final_stage"] += 1
                for assignment in repo.list_assignments(node.node_id):
                    if assignment.status == "ready":
                        assignment.status = "cancelled"
                        assignment.completed_at = now
                        summary["assignments_cancelled"] += 1
                    elif assignment.status == "running":
                        assignment.status = "interrupted"
                        assignment.completed_at = now
                        summary["assignments_interrupted"] += 1
                for worker in repo.list_workers(node.node_id):
                    if worker.status != "retired":
                        worker.status = "retired"
                        worker.retired_at = now
                        summary["workers_retired"] += 1
                if node.status not in TERMINAL_NODE_STATES:
                    details = dict(node.details or {})
                    target_status = str(details.get("drain_target_status") or "retired")
                    if target_status not in TERMINAL_NODE_STATES:
                        target_status = "retired"
                    node.status = target_status
                    node.node_version += 1
                    summary["nodes_reconciled"] += 1
            repo.append_event(
                "run",
                run_id,
                "run_final_state_reconciled",
                "Writer 启动前已封口所有 Node、Assignment 与 Worker",
                summary,
            )
            active_nodes = int(session.scalar(select(func.count()).select_from(ResearchNodeRecord).where(
                ResearchNodeRecord.run_id == run_id,
                ResearchNodeRecord.status.in_(["ready", "active", "draining"]),
            )) or 0)
            active_assignments = int(session.scalar(select(func.count()).select_from(WorkerAssignmentRecord).where(
                WorkerAssignmentRecord.node_id.in_(select(ResearchNodeRecord.node_id).where(ResearchNodeRecord.run_id == run_id)),
                WorkerAssignmentRecord.status.in_(["ready", "running"]),
            )) or 0)
            active_workers = int(session.scalar(select(func.count()).select_from(WorkerIdentityRecord).where(
                WorkerIdentityRecord.node_id.in_(select(ResearchNodeRecord.node_id).where(ResearchNodeRecord.run_id == run_id)),
                WorkerIdentityRecord.status.in_(["assigned", "running", "draining"]),
            )) or 0)
            if active_nodes or active_assignments or active_workers:
                raise DynamicProverError(
                    "Run 终态封口失败："
                    f"active_nodes={active_nodes}, active_assignments={active_assignments}, active_workers={active_workers}"
                )

    # ------------------------------------------------------------------ mathematical paper publication layer
    async def _write_final_report(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        completion_kind: str,
        *,
        model_profile_override: dict[str, dict[str, str]] | None = None,
        output_root: Path | None = None,
        publication_scope: str | None = None,
        record_run_phases: bool = True,
        usage_component: str = "prover",
        reuse_existing_v1: bool = False,
    ) -> Path:
        """以精选数学语料生成英文 TeX，经真实编译和 fresh 审稿后发布 PDF。"""
        del completion_kind
        final_root = output_root or self._run_root(run_id) / "final"
        scope_id = publication_scope or run_id
        scope_type = "publication_replay" if publication_scope else "run"
        with self.database.session() as session:
            run_record = session.get(ResearchRunRecord, run_id)
            recovering_paused = bool(
                (run_record is not None and str(run_record.pre_pause_phase or "").startswith("paper_"))
                or session.scalar(
                select(func.count()).select_from(AgentOperationCheckpointRecord).where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.status == "paused",
                    AgentOperationCheckpointRecord.pause_epoch == (
                        run_record.pause_epoch if run_record is not None else -1
                    ),
                    AgentOperationCheckpointRecord.operation_kind.in_([
                        "paper_writer_v1", "paper_auditor_v1", "paper_writer_v2",
                        "publication_scout_adversarial_prior_art_scout",
                        "publication_scout_related_work_context_scout",
                    ]),
                )
            ))
        published_tex = final_root / "paper.tex"
        published_pdf = final_root / "paper.pdf"
        if recovering_paused and published_tex.is_file() and published_pdf.is_file():
            return published_pdf
        if record_run_phases and not self._run_has_event(run_id, "paper_writing_started", attempt_id):
            self._record_run_phase(
                run_id,
                "paper_writing_started",
                "研究阶段已收束，Writer 正在根据当前数学结果撰写论文",
                attempt_id=attempt_id,
            )
        corpus = build_paper_source_corpus(
            self.database,
            self.artifacts,
            run_id,
            output_root=(
                final_root / "paper-source-corpus"
                if publication_scope
                else self._run_runtime_root(run_id) / "publication" / "paper-source-corpus"
            ),
        )
        task = f"""# Write the user-facing mathematical manuscript as LaTeX

The curated Paper Source Corpus is mounted at:

`input/source_materials/paper_sources/`

Read `00-paper-source-manifest.md` first. Use the curated mathematical files as the
main source. Open the bounded raw mathematical archive only when a proof,
mathematical obstruction, source citation, or remaining analytic interface needs more
detail.

Output only one complete, self-contained English LaTeX document. Do not use Markdown
or code fences. Choose a normal LaTeX preamble and any installed packages genuinely
needed by the manuscript. Organize the paper by
mathematical logic rather than discovery chronology. It must contain an abstract,
`\\section{{Introduction}}`, clear theorem environments and proof environments when
the corpus supports them, and an English Discussion or Concluding Remarks section.
References are optional and should remain self-contained. Do not read external TeX,
BibTeX, image, or data files. The Controller accepts the manuscript only after a real
no-shell-escape XeLaTeX build produces a readable PDF.

Do not mention MathLab, Research Run, Research Contract, Research Node, Worker,
Supervisor, Architecture Reviewer, Finding, Promotion, Fresh Verifier, Evidence
Manifest, budget, completion status, internal IDs, or the chronology of the research
system. Do not append an audit, provenance appendix, evidence table, or run summary.
"""

        first_build: LatexCompilation | None = None

        def first_paper_parser(text: str) -> tuple[str | None, ValidationResult]:
            nonlocal first_build
            build, errors = self.latex_compiler.compile(
                text,
                require_theorem=corpus.verified_result_count > 0,
                genuine_source_count=corpus.genuine_source_count,
            )
            if errors or build is None:
                return None, ValidationResult(errors, [], format_name="LaTeX")
            first_build = build
            return text, ValidationResult([], [], format_name="LaTeX")

        existing_v1_tex = final_root / "paper-v001.tex"
        existing_v1_pdf = final_root / "paper-v001.pdf"
        reuse_publication_artifacts = recovering_paused or reuse_existing_v1
        if reuse_publication_artifacts and existing_v1_tex.is_file() and existing_v1_pdf.is_file():
            first_paper = existing_v1_tex.read_text(encoding="utf-8")
            first_build, first_errors = self.latex_compiler.compile(
                first_paper,
                require_theorem=corpus.verified_result_count > 0,
                genuine_source_count=corpus.genuine_source_count,
            )
            if first_errors or first_build is None:
                raise DynamicProverError("暂停前 Writer v1 产物无法重新通过本地编译门禁")
        else:
            first_paper, _ = await self._validated_role(
                user_id=user_id,
                run_id=run_id,
                attempt_id=attempt_id,
                node_id=None,
                role="paper_writer",
                role_key="paper_writer",
                scope_type=scope_type,
                scope_id=scope_id,
                task=task,
                parser=first_paper_parser,
                persistent=True,
                context_paths=list(corpus.writer_context_paths),
                model_profile_override=model_profile_override,
                output_format="latex",
                usage_component=usage_component,
                operation_kind="paper_writer_v1",
                checkpoint_payload={"publication_scope": scope_id, "stage": "writer_v1"},
            )
        if first_build is None:
            raise DynamicProverError("Writer 首稿通过但缺少 XeLaTeX 编译结果")
        v1_tex, v1_pdf, _ = self._persist_latex_compilation(final_root, "paper-v001", first_build)

        if record_run_phases and not self._run_has_event(run_id, "paper_audit_started", attempt_id):
            self._record_run_phase(
                run_id,
                "paper_audit_started",
                "论文初稿已形成，Fresh Auditor 与两名独立文献审读者正在并行工作",
                attempt_id=attempt_id,
            )
        audit_task = """# Fresh audit of the mathematical LaTeX manuscript

The Writer's curated corpus and your internal verification-boundary material are
mounted at:

`input/source_materials/paper_sources/`

Read `paper-v001.tex`, the Paper Source Manifest, theorem-ready proof material, and
the internal verification-boundary files needed to check the manuscript. The TeX has
already compiled successfully. This audit is internal and must be written in the
required Markdown audit format.
Do not require run status, budget, internal IDs, provenance appendices, or Evidence
Manifest language to appear in the paper. Treat any such language in the manuscript
as a genre violation. Verify that all prose in the manuscript is English.
"""
        audit_path = final_root / "paper-audit-v001.md"

        async def run_audit() -> tuple[str, AgentTurn] | dict[str, str]:
            if recovering_paused and audit_path.is_file():
                return {"audit": audit_path.read_text(encoding="utf-8")}
            result = await self._validated_role(
                user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=None,
                role="paper_auditor", role_key="paper_auditor:v1",
                scope_type="paper_audit", scope_id=f"{scope_id}-v1", task=audit_task,
                parser=self._validate_paper_audit, persistent=False,
                context_paths=[*corpus.auditor_context_paths, v1_tex, v1_pdf],
                model_profile_override=model_profile_override,
                usage_component=usage_component,
                operation_kind="paper_auditor_v1",
                checkpoint_payload={"publication_scope": scope_id, "stage": "audit_v1"},
            )
            self.artifacts.write_text(self.artifacts.reference(audit_path), result[0])
            return result

        audit_call = run_audit()
        prior_call = self._resume_or_run_publication_scout(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id,
            publication_scope=scope_id, final_root=final_root,
            role="adversarial_prior_art_scout", v1_tex=v1_tex,
            model_profile_override=model_profile_override, usage_component=usage_component,
            reuse_existing=reuse_publication_artifacts,
        )
        context_call = self._resume_or_run_publication_scout(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id,
            publication_scope=scope_id, final_root=final_root,
            role="related_work_context_scout", v1_tex=v1_tex,
            model_profile_override=model_profile_override, usage_component=usage_component,
            reuse_existing=reuse_publication_artifacts,
        )
        audit_result, prior_result, context_result = await asyncio.gather(
            audit_call, prior_call, context_call, return_exceptions=True
        )
        if isinstance(audit_result, BaseException):
            raise audit_result
        audit = str(audit_result.get("audit") or "") if isinstance(audit_result, dict) else audit_result[0]
        if not audit_path.is_file():
            self.artifacts.write_text(self.artifacts.reference(audit_path), audit)
        scout_results = self._settle_publication_scouts(
            final_root,
            {
                "adversarial_prior_art_scout": prior_result,
                "related_work_context_scout": context_result,
            },
        )
        revision_inputs = self._publication_revision_inputs(
            final_root, v1_tex, audit_path, scout_results
        )
        scout_source_total = sum(
            int(item.get("source_count") or 0) for item in scout_results.values()
        )

        if record_run_phases and not self._run_has_event(run_id, "paper_revision_started", attempt_id):
            self._record_run_phase(
                run_id,
                "paper_revision_started",
                "审稿与独立文献意见已形成，Writer 正在修订最终论文",
                attempt_id=attempt_id,
            )
        revision_task = """# Revise the same mathematical LaTeX manuscript

The Paper Source Corpus remains mounted at:

`input/source_materials/paper_sources/`

Read `paper-v001.tex`, `paper-audit-v001.md`, and both publication literature Scout
directories when present. Revise that exact paper in response to the fresh audit and
the independently verified literature reports while preserving its mathematical-paper
genre. Integrate genuine prior art naturally into the
Introduction, Related Work, or Discussion; never expose Scout names, evidence-level
labels, local paths, or publication workflow in the manuscript. Submit only the complete
raw LaTeX document, without Markdown fences, revision decisions, run metadata,
provenance, audit commentary, or internal identifiers. Do not create a new substantive
theorem merely to answer the reviews; instead narrow, conditionalize, omit, or discuss
honestly when the supplied evidence does not support a claim.
"""
        revised_build: LatexCompilation | None = None

        def revised_paper_parser(text: str) -> tuple[str | None, ValidationResult]:
            nonlocal revised_build
            build, errors = self.latex_compiler.compile(
                text,
                require_theorem=corpus.verified_result_count > 0,
                genuine_source_count=corpus.genuine_source_count + scout_source_total,
            )
            if errors or build is None:
                return None, ValidationResult(errors, [], format_name="LaTeX")
            revised_build = build
            return text, ValidationResult([], [], format_name="LaTeX")

        existing_v2_tex = final_root / "paper-v002.tex"
        existing_v2_pdf = final_root / "paper-v002.pdf"
        if reuse_publication_artifacts and existing_v2_tex.is_file() and existing_v2_pdf.is_file():
            revised = existing_v2_tex.read_text(encoding="utf-8")
            revised_build, revised_errors = self.latex_compiler.compile(
                revised,
                require_theorem=corpus.verified_result_count > 0,
                genuine_source_count=corpus.genuine_source_count + scout_source_total,
            )
            if revised_errors or revised_build is None:
                raise DynamicProverError("暂停前 Writer v2 产物无法重新通过本地编译门禁")
        else:
            revised, _ = await self._validated_role(
                user_id=user_id,
                run_id=run_id,
                attempt_id=attempt_id,
                node_id=None,
                role="paper_writer",
                role_key="paper_writer",
                scope_type=scope_type,
                scope_id=scope_id,
                task=revision_task,
                parser=revised_paper_parser,
                persistent=True,
                context_paths=[*corpus.writer_context_paths, revision_inputs],
                model_profile_override=model_profile_override,
                output_format="latex",
                usage_component=usage_component,
                operation_kind="paper_writer_v2",
                checkpoint_payload={"publication_scope": scope_id, "stage": "writer_v2"},
            )
        if revised_build is None:
            raise DynamicProverError("Writer 修订稿通过但缺少 XeLaTeX 编译结果")
        v2_tex, v2_pdf, _ = self._persist_latex_compilation(final_root, "paper-v002", revised_build)

        final_tex = final_root / "paper.tex"
        final_pdf = final_root / "paper.pdf"
        self.artifacts.write_text(self.artifacts.reference(final_tex), revised)
        self.artifacts.write_bytes(self.artifacts.reference(final_pdf), revised_build.pdf_bytes)
        self.artifacts.write_json(
            self.artifacts.reference(final_root / "publication-manifest.json"),
            {
                "schema_version": 3,
                "language": "English",
                "engine": "XeLaTeX",
                "compiler_version": revised_build.compiler_version,
                "draft": {
                    "tex_path": self.artifacts.reference(v1_tex),
                    "pdf_path": self.artifacts.reference(v1_pdf),
                    "tex_sha256": first_build.source_sha256,
                    "pdf_sha256": first_build.pdf_sha256,
                    "pages": first_build.page_count,
                },
                "audit_path": self.artifacts.reference(audit_path),
                "publication_literature_reviews": scout_results,
                "revision": {
                    "tex_path": self.artifacts.reference(v2_tex),
                    "pdf_path": self.artifacts.reference(v2_pdf),
                    "tex_sha256": revised_build.source_sha256,
                    "pdf_sha256": revised_build.pdf_sha256,
                    "pages": revised_build.page_count,
                },
                "published_tex_path": self.artifacts.reference(final_tex),
                "published_pdf_path": self.artifacts.reference(final_pdf),
            },
        )
        if record_run_phases and not self._run_has_event(run_id, "paper_published", attempt_id):
            self._record_run_phase(
                run_id,
                "paper_published",
                "最终 TeX 与 PDF 已通过编译并发布",
                attempt_id=attempt_id,
            )
        return final_pdf

    def _record_run_phase(
        self,
        run_id: str,
        kind: str,
        message: str,
        *,
        attempt_id: str | None = None,
    ) -> None:
        """把论文阶段写入内部 Run 事件，供刷新后恢复状态但不进入 Streaming Events。"""
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).append_event(
                "run", run_id, kind, message, {"attempt_id": attempt_id}
            )

    def _run_has_event(self, run_id: str, kind: str, attempt_id: str | None) -> bool:
        """判断当前 Attempt 是否已经持久进入某个出版阶段。"""
        with self.database.session() as session:
            events = RepositoryHub(session, self.artifacts).read_events("run", run_id)
            return any(
                item.kind == kind
                and (
                    attempt_id is None
                    or str((item.payload or {}).get("attempt_id") or "") == attempt_id
                )
                for item in events
            )

    def _persist_latex_compilation(
        self,
        final_root: Path,
        stem: str,
        build: LatexCompilation,
    ) -> tuple[Path, Path, Path]:
        """原子保存一版 TeX/PDF，并把编译日志留在内部 build 目录。"""
        tex_path = final_root / f"{stem}.tex"
        pdf_path = final_root / f"{stem}.pdf"
        log_path = final_root / f"build-{stem.removeprefix('paper-')}" / "latexmk.log"
        self.artifacts.write_text(self.artifacts.reference(tex_path), build.source)
        self.artifacts.write_bytes(self.artifacts.reference(pdf_path), build.pdf_bytes)
        self.artifacts.write_text(self.artifacts.reference(log_path), build.log)
        return tex_path, pdf_path, log_path

    @staticmethod
    def _validate_paper_audit(text: str) -> tuple[str | None, ValidationResult]:
        """核验内部稿件审计的固定栏目和明确 verdict。"""
        normalized = DynamicResearchController._normalize_markdown(text)
        errors: list[str] = []
        for label in (
            "## Verdict", "## Mathematical Correctness", "## Evidence Boundary",
            "## Manuscript Genre", "## References", "## Required Revisions",
        ):
            if label not in normalized:
                errors.append(f"内部论文审计缺少 `{label}`。")
        if not re.search(r"\b(PASS|REVISE|REJECT)\b", normalized):
            errors.append("内部论文审计 Verdict 必须为 PASS/REVISE/REJECT。")
        return (normalized if not errors else None, ValidationResult(errors, []))

    def _publication_literature_settings(self) -> PublicationLiteratureConfig:
        """读取 backend 配置；纯离线/测试 backend 使用与正式配置相同的默认值。"""
        config = getattr(self.backend, "config", None)
        publication = getattr(config, "publication_literature", None)
        if publication is not None:
            return publication
        return PublicationLiteratureConfig(True, 6, 6, 30, 4, 5)

    def _publication_scout_limits(self) -> LiteratureLimits:
        """读取 publication-only 文献预算；研究 Worker 继续使用原有较小预算。"""
        publication = self._publication_literature_settings()
        return LiteratureLimits(
            theorem_searches=publication.theorem_searches,
            arxiv_searches=publication.arxiv_searches,
            abstracts=publication.abstract_candidates,
            papers=publication.fulltext_papers,
        )

    async def _run_publication_scout(
        self,
        *,
        user_id: str,
        run_id: str,
        attempt_id: str,
        publication_scope: str,
        final_root: Path,
        role: str,
        v1_tex: Path,
        model_profile_override: dict[str, dict[str, str]] | None,
        usage_component: str = "prover",
    ) -> dict[str, object]:
        """让一个 fresh Scout 只读 v1，独立检索并冻结其完整文献证据。"""
        if role not in {"adversarial_prior_art_scout", "related_work_context_scout"}:
            raise ValueError(f"未知 publication Scout：{role}")
        publication = self._publication_literature_settings()
        if not publication.enabled:
            return {"role": role, "status": "disabled", "source_count": 0}
        scout_root = final_root / "scouts" / role
        archive_root = scout_root / "literature"
        task = f"""# Independently review the literature around paper-v1.tex

Read only `input/source_materials/paper-v1.tex` as project mathematical context.
Use the Controller-provided theorem/arXiv/fetch/paper-reader tools to perform the
publication literature investigation required by your role. You may inspect broad
search and abstract candidates, but deeply materialize at most
{publication.fulltext_papers} successful exact-version papers. Failed downloads do
not consume that successful-paper limit. Keep at most about
{publication.abstract_keep} genuinely useful abstract-only sources in the final report.

Submit one complete human-readable Markdown report using only the evidence classes
`FULL_TEXT_VERIFIED` and `ABSTRACT_VERIFIED`. Every cited arXiv item must include its
real title, authors, arXiv ID, canonical link, and exact version when deeply read.
Do not request or infer any Contract, Claim, Research Node, Worker, Supervisor, or
other internal research material.
"""

        def parser(text: str) -> tuple[str | None, ValidationResult]:
            if self.settings.offline_agents:
                return text.strip() + "\n", ValidationResult([], [])
            return validate_scout_markdown(role, text)

        def evidence_validator(
            parsed: str | None,
            turn: AgentTurn,
            raw_text: str,
        ) -> ValidationResult:
            return validate_scout_evidence(parsed or raw_text, turn.evidence)

        report, turn = await self._validated_role(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=None,
            role=role, role_key=role, scope_type="publication_scout",
            scope_id=f"{publication_scope}-{role}", task=task, parser=parser,
            persistent=False, literature_access=not self.settings.offline_agents,
            literature_limits=self._publication_scout_limits(),
            literature_archive_root=archive_root, context_paths=[v1_tex],
            model_profile_override=model_profile_override,
            usage_component=usage_component,
            turn_validator=None if self.settings.offline_agents else evidence_validator,
            operation_kind=f"publication_scout_{role}",
            checkpoint_payload={"publication_scope": publication_scope, "stage": role},
        )
        evidence = dict(turn.evidence or {})
        operation_root = Path(str(evidence.get("operation_root") or "")).resolve()
        operation_literature = operation_root / "literature"
        if operation_literature.is_dir():
            archive_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(operation_literature, archive_root, dirs_exist_ok=True)
        report_path = scout_root / "report.md"
        self.artifacts.write_text(self.artifacts.reference(report_path), report)
        result = {
            "role": role,
            "status": "completed",
            "report_path": self.artifacts.reference(report_path),
            "literature_path": self.artifacts.reference(archive_root),
            "source_count": scout_source_count(report),
            "model": turn.model,
            "reasoning_effort": turn.reasoning_effort,
            "thread_id": turn.thread_id,
            "usage": dict(turn.usage),
            "network_failures": int(evidence.get("network_failures") or 0),
        }
        self.artifacts.write_json(
            self.artifacts.reference(scout_root / "result.json"), result
        )
        return result

    async def _resume_or_run_publication_scout(
        self, *, reuse_existing: bool, **kwargs: Any
    ) -> dict[str, object]:
        """复用已完成 Scout 结果；缺失时才恢复对应 fresh operation。"""
        final_root = Path(kwargs["final_root"])
        role = str(kwargs["role"])
        result_path = final_root / "scouts" / role / "result.json"
        if reuse_existing and result_path.is_file():
            return dict(self.artifacts.read_json(self.artifacts.reference(result_path)))
        return await self._run_publication_scout(**kwargs)

    def _settle_publication_scouts(
        self,
        final_root: Path,
        results: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        """持久记录 Scout 成功或真实失败；失败不伪造报告，出版链可显式降级。"""
        settled: dict[str, dict[str, object]] = {}
        for role, result in results.items():
            if isinstance(result, BaseException):
                record: dict[str, object] = {
                    "role": role,
                    "status": "failed",
                    "source_count": 0,
                    "error": f"{type(result).__name__}: {result}",
                }
                failure_path = final_root / "scouts" / role / "failure.json"
                self.artifacts.write_json(self.artifacts.reference(failure_path), record)
                record["failure_path"] = self.artifacts.reference(failure_path)
                settled[role] = record
                continue
            settled[role] = dict(result)
        return settled

    def _publication_revision_inputs(
        self,
        final_root: Path,
        v1_tex: Path,
        audit_path: Path,
        scout_results: dict[str, dict[str, object]],
    ) -> Path:
        """为 Writer revision 构造无碰撞的显式输入树，不暴露 Scout 工作区。"""
        target = final_root / "revision-inputs"
        staging = final_root / ".revision-inputs.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        shutil.copy2(v1_tex, staging / "paper-v001.tex")
        shutil.copy2(audit_path, staging / "paper-audit-v001.md")
        for role, result in scout_results.items():
            if result.get("status") != "completed":
                continue
            report_path = self.artifacts.resolve(str(result["report_path"]))
            literature_path = self.artifacts.resolve(str(result["literature_path"]))
            role_root = staging / role
            role_root.mkdir()
            shutil.copy2(report_path, role_root / "report.md")
            if literature_path.is_dir():
                shutil.copytree(literature_path, role_root / "literature")
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        return target

    async def republish_completed_run(
        self,
        user_id: str,
        run_id: str,
        *,
        writer_model: str,
        writer_effort: str,
        auditor_model: str,
        auditor_effort: str,
        delete_legacy_markdown: bool = False,
    ) -> Path:
        """只重跑已完成 Run 的出版层，并把模型覆盖与结果记录在独立清单中。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status != "completed" or not run.attempts:
                raise DynamicProverError("只有已完成且具有原始 Attempt 的 Run 可以重新出版")
            if run.claimed_by_runner:
                raise DynamicProverError("Run 当前仍被 Runner 占用，拒绝并发重新出版")
            attempt_id = run.attempts[-1].attempt_id
            completion_kind = str(run.completion_kind or "completed")

        final_root = self._run_root(run_id) / "final"
        existing = sorted(final_root.glob("publication-attempt-v*.json"))
        sequence = len(existing) + 1
        manifest_path = final_root / f"publication-attempt-v{sequence:03d}.json"
        model_profile = {
            "paper_writer": {"model": writer_model, "reasoning_effort": writer_effort},
            "paper_auditor": {"model": auditor_model, "reasoning_effort": auditor_effort},
        }
        manifest: dict[str, object] = {
            "schema_version": 2,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "status": "running",
            "started_at": utc_now().isoformat(),
            "models": model_profile,
            "legacy_markdown_deletion_requested": bool(delete_legacy_markdown),
        }
        self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
        try:
            paper_path = await self._write_final_report(
                user_id,
                run_id,
                attempt_id,
                completion_kind,
                model_profile_override=model_profile,
            )
            source_path = paper_path.with_suffix(".tex")
            deleted = (
                self._delete_legacy_publication_markdown(run_id)
                if delete_legacy_markdown
                else []
            )
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                run = repo.get_run(user_id, run_id)
                run.final_paper_pdf_path = self.artifacts.reference(paper_path)
                run.final_paper_tex_path = self.artifacts.reference(source_path)
                if delete_legacy_markdown and run.final_report_path:
                    legacy_reference = str(run.final_report_path)
                    if legacy_reference in deleted or not self.artifacts.resolve(legacy_reference).is_file():
                        run.final_report_path = None
                repo.append_event(
                    "run",
                    run_id,
                    "mathematical_paper_republished",
                    "已复用既有数学研究材料重新生成最终数学论文",
                    {
                        "paper_pdf_path": self.artifacts.reference(paper_path),
                        "paper_tex_path": self.artifacts.reference(source_path),
                        "models": model_profile,
                        "deleted_legacy_markdown": deleted,
                    },
                )
            manifest.update(
                {
                    "status": "completed",
                    "completed_at": utc_now().isoformat(),
                    "paper_pdf_path": self.artifacts.reference(paper_path),
                    "paper_tex_path": self.artifacts.reference(source_path),
                    "deleted_legacy_markdown": deleted,
                }
            )
            self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
            return paper_path
        except Exception as exc:
            manifest.update(
                {
                    "status": "failed",
                    "completed_at": utc_now().isoformat(),
                    "error": str(exc),
                }
            )
            self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
            raise

    async def replay_completed_run_publication(
        self,
        user_id: str,
        run_id: str,
        *,
        replay_id: str | None = None,
        resume_existing: bool = False,
    ) -> Path:
        """从冻结 Prover 产物重跑完整出版层，并保证历史 final 与 Run 状态不变。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status != "completed" or not run.attempts:
                raise DynamicProverError("只有已完成且具有原始 Attempt 的 Run 可以做出版 replay")
            if run.claimed_by_runner:
                raise DynamicProverError("Run 当前仍被 Runner 占用，拒绝并发出版 replay")
            attempt_id = run.attempts[-1].attempt_id
            completion_kind = str(run.completion_kind or "completed")
            frozen_profile = dict((run.execution_policy or {}).get("resolved_role_models") or {})
            model_profile = {
                "paper_writer": dict(frozen_profile.get("paper_writer") or {}),
                "paper_auditor": dict(frozen_profile.get("paper_auditor") or {}),
            }
            if not all(item.get("model") and item.get("reasoning_effort") for item in model_profile.values()):
                raise DynamicProverError("历史 Run 缺少冻结的 Writer/Auditor 模型配置")
            original_state = {
                "status": run.status,
                "completion_kind": run.completion_kind,
                "final_paper_tex_path": run.final_paper_tex_path,
                "final_paper_pdf_path": run.final_paper_pdf_path,
            }

        replay_key = replay_id or new_id("publication_replay")
        replay_root = self._run_root(run_id) / "runtime" / "publication-replays" / replay_key
        if replay_root.exists() and not resume_existing:
            raise DynamicProverError(f"出版 replay 已存在：{replay_key}")
        replay_root.mkdir(parents=True, exist_ok=resume_existing)
        historical_final = self._run_root(run_id) / "final"
        historical_digest = self._publication_tree_digest(historical_final)
        manifest_path = replay_root / "replay-manifest.json"
        if resume_existing:
            if not manifest_path.is_file():
                raise DynamicProverError("恢复出版 replay 时缺少 replay-manifest.json")
            manifest = dict(self.artifacts.read_json(self.artifacts.reference(manifest_path)))
            if manifest.get("run_id") != run_id or manifest.get("replay_id") != replay_key:
                raise DynamicProverError("恢复出版 replay 的 manifest 身份不匹配")
            if manifest.get("historical_final_sha256") != historical_digest:
                raise DynamicProverError("恢复前历史 final 已发生变化")
            manifest.update(
                {
                    "status": "running",
                    "resumed_at": utc_now().isoformat(),
                    "resume_count": int(manifest.get("resume_count") or 0) + 1,
                    "error": None,
                }
            )
        else:
            manifest = {
                "schema_version": 1,
                "replay_id": replay_key,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "status": "running",
                "started_at": utc_now().isoformat(),
                "models": {
                    **model_profile,
                    "adversarial_prior_art_scout": model_profile["paper_auditor"],
                    "related_work_context_scout": model_profile["paper_auditor"],
                },
                "historical_final_sha256": historical_digest,
                "historical_run_state": original_state,
            }
        self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
        try:
            paper_path = await self._write_final_report(
                user_id,
                run_id,
                attempt_id,
                completion_kind,
                model_profile_override=model_profile,
                output_root=replay_root,
                publication_scope=replay_key,
                record_run_phases=False,
                usage_component="publication_replay",
            )
            if self._publication_tree_digest(historical_final) != historical_digest:
                raise DynamicProverError("出版 replay 改写了历史 final 产物")
            with self.database.session() as session:
                run = RepositoryHub(session, self.artifacts).get_run(user_id, run_id)
                current_state = {
                    "status": run.status,
                    "completion_kind": run.completion_kind,
                    "final_paper_tex_path": run.final_paper_tex_path,
                    "final_paper_pdf_path": run.final_paper_pdf_path,
                }
            if current_state != original_state:
                raise DynamicProverError("出版 replay 改写了历史 Run 状态或最终论文指针")
            old_tex = historical_final / "paper.tex"
            new_tex = paper_path.with_suffix(".tex")
            comparison_path = replay_root / "comparison.md"
            self.artifacts.write_text(
                self.artifacts.reference(comparison_path),
                "\n".join(
                    [
                        "# Publication Literature Replay Comparison",
                        "",
                        f"Run: `{run_id}`",
                        f"Replay: `{replay_key}`",
                        "",
                        "## Historical final paper",
                        "",
                        f"TeX SHA-256: `{sha256_file(old_tex) if old_tex.is_file() else 'missing'}`",
                        "",
                        "## New publication replay",
                        "",
                        f"TeX SHA-256: `{sha256_file(new_tex)}`",
                        f"PDF: `{self.artifacts.reference(paper_path)}`",
                        "",
                        "The replay regenerated Writer v1 from the frozen Prover corpus, ran the",
                        "Fresh Auditor and two independent literature Scouts concurrently, and",
                        "used the same replay Writer thread for v2. Historical final artifacts",
                        "and Research Run state were not modified.",
                        "",
                    ]
                ),
            )
            manifest.update(
                {
                    "status": "completed",
                    "completed_at": utc_now().isoformat(),
                    "paper_tex_path": self.artifacts.reference(new_tex),
                    "paper_pdf_path": self.artifacts.reference(paper_path),
                    "comparison_path": self.artifacts.reference(comparison_path),
                    "historical_final_unchanged": True,
                }
            )
            self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
            return replay_root
        except Exception as exc:
            manifest.update(
                {
                    "status": "failed",
                    "completed_at": utc_now().isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.artifacts.write_json(self.artifacts.reference(manifest_path), manifest)
            raise

    async def recover_interrupted_run_publication(
        self,
        user_id: str,
        run_id: str,
        *,
        recovery_id: str | None = None,
        reuse_saved_writer: bool = True,
    ) -> Path:
        """从已封口的 interrupted/出版技术失败 Run 恢复出版并晋升完成。

        恢复入口只接受已经写入 ``run_final_state_reconciled``、且所有研究
        Node、Assignment 与 Worker 都已终态化的 Run。出版角色使用新的
        recovery scope，不复用被中断的 Writer thread；成功后沿正常终局路径
        写入 TeX/PDF、结算费用并导出公开快照。
        """
        recovery_key = recovery_id or new_id("publication_recovery")
        model_profile: dict[str, dict[str, str]]
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status not in {"interrupted", "failed"} or not run.attempts:
                raise DynamicProverError("只有研究阶段已封口但出版中断/技术失败的 Run 可以恢复出版")
            if run.final_paper_pdf_path or run.final_paper_tex_path:
                raise DynamicProverError("Run 已存在最终论文指针，拒绝重复恢复出版")
            if not any(
                item.kind == "run_final_state_reconciled"
                for item in repo.read_events("run", run_id)
            ):
                raise DynamicProverError("Run 尚未完成研究终态封口，拒绝单独恢复出版")
            run_events = repo.read_events("run", run_id)
            if run.status == "failed" and not any(
                item.kind in {"paper_writing_started", "paper_audit_started", "paper_revision_started"}
                for item in run_events
            ):
                raise DynamicProverError("该 failed Run 未进入出版阶段，拒绝按出版技术失败恢复")
            nonterminal_nodes = [
                node.node_id for node in repo.list_nodes(run_id)
                if node.status not in TERMINAL_NODE_STATES
            ]
            active_assignments: list[str] = []
            active_workers: list[str] = []
            for node in repo.list_nodes(run_id):
                active_assignments.extend(
                    item.assignment_id for item in repo.list_assignments(node.node_id)
                    if item.status in {"ready", "running"}
                )
                active_workers.extend(
                    item.worker_id for item in repo.list_workers(node.node_id)
                    if item.status in {"assigned", "running", "draining"}
                )
            if nonterminal_nodes or active_assignments or active_workers:
                raise DynamicProverError(
                    "Run 仍含活动研究对象，拒绝单独恢复出版："
                    f"nodes={len(nonterminal_nodes)}, assignments={len(active_assignments)}, "
                    f"workers={len(active_workers)}"
                )
            final_root = self._run_root(run_id) / "final"
            if reuse_saved_writer and (
                (final_root / "paper.tex").exists() or (final_root / "paper.pdf").exists()
            ):
                raise DynamicProverError("Run final 目录已有未登记论文，需先人工审计后再恢复")
            frozen_profile = dict((run.execution_policy or {}).get("resolved_role_models") or {})
            model_profile = {
                "paper_writer": dict(frozen_profile.get("paper_writer") or {}),
                "paper_auditor": dict(frozen_profile.get("paper_auditor") or {}),
            }
            if not all(
                item.get("model") and item.get("reasoning_effort")
                for item in model_profile.values()
            ):
                raise DynamicProverError("Interrupted Run 缺少冻结的 Writer/Auditor 模型配置")

            saved_writer_checkpoint = session.scalar(
                select(AgentOperationCheckpointRecord)
                .where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.role == "paper_writer",
                    AgentOperationCheckpointRecord.provider_thread_id.is_not(None),
                )
                .order_by(AgentOperationCheckpointRecord.updated_at.desc())
                .limit(1)
            )
            saved_writer_thread = (
                saved_writer_checkpoint.provider_thread_id
                if reuse_saved_writer and saved_writer_checkpoint is not None
                else None
            )

        reuse_saved_v1 = False
        saved_v1_source: Path | None = None
        if reuse_saved_writer and saved_writer_checkpoint is not None:
            saved_workspace = (
                self._run_runtime_root(run_id)
                / "agents"
                / (
                    f"{saved_writer_checkpoint.scope_type}-"
                    f"{saved_writer_checkpoint.scope_id}-"
                    f"{saved_writer_checkpoint.role_key.replace(':', '-')}"
                )
                / saved_writer_checkpoint.role
            )
            candidate = saved_workspace / "paper.tex"
            if candidate.is_file():
                candidate_text = candidate.read_text(encoding="utf-8")
                corpus = build_paper_source_corpus(
                    self.database, self.artifacts, run_id,
                    output_root=self._run_runtime_root(run_id) / "publication" / "paper-source-corpus",
                )
                saved_build, saved_errors = self.latex_compiler.compile(
                    candidate_text,
                    require_theorem=corpus.verified_result_count > 0,
                    genuine_source_count=corpus.genuine_source_count,
                )
                if saved_errors or saved_build is None:
                    raise DynamicProverError(
                        "失败 Writer 留下的 paper.tex 无法通过出版恢复编译门禁："
                        + "；".join(saved_errors)
                    )
                final_root = self._run_root(run_id) / "final"
                final_root.mkdir(parents=True, exist_ok=True)
                self._persist_latex_compilation(final_root, "paper-v001", saved_build)
                reuse_saved_v1 = True
                saved_v1_source = candidate

        if not reuse_saved_writer:
            final_root = self._run_root(run_id) / "final"
            archive_root = (
                self._run_root(run_id)
                / "runtime"
                / "publication-recovery-history"
                / recovery_key
            )
            publication_entries = (
                "paper-v001.tex", "paper-v001.pdf", "paper-v002.tex", "paper-v002.pdf",
                "paper-audit-v001.md", "paper-source-corpus", "build-v001", "build-v002",
                "scouts", "revision-inputs", "publication-manifest.json", "paper.tex", "paper.pdf",
            )
            moved: list[str] = []
            for name in publication_entries:
                source = final_root / name
                if not source.exists():
                    continue
                archive_root.mkdir(parents=True, exist_ok=True)
                target = archive_root / name
                if target.exists():
                    raise DynamicProverError(f"出版重启归档目标已存在：{target}")
                shutil.move(str(source), str(target))
                moved.append(name)
            if moved:
                self.artifacts.write_json(
                    self.artifacts.reference(archive_root / "archive-manifest.json"),
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "recovery_id": recovery_key,
                        "archived_at": utc_now().isoformat(),
                        "entries": moved,
                    },
                )

        preflight = getattr(self.backend, "preflight_literature", None)
        if callable(preflight):
            try:
                await preflight()
            except Exception as exc:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).append_event(
                        "run",
                        run_id,
                        "literature_preflight_warning",
                        "出版恢复期间文献预检暂不可用；恢复流程继续",
                        {"error": str(exc), "publication_recovery_id": recovery_key},
                    )

        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status not in {"interrupted", "failed"}:
                raise DynamicProverError("出版恢复启动前 Run 状态已变化")
            latest_attempt_number = max(
                (item.attempt_number for item in run.attempts), default=0
            )
            attempt = RunAttemptRecord(
                attempt_id=new_id("attempt"),
                run_id=run_id,
                attempt_number=latest_attempt_number + 1,
                status="running",
                started_at=utc_now(),
            )
            session.add(attempt)
            run.status = "draining"
            run.completed_at = None
            run.terminal_at = None
            run.runtime_expires_at = None
            run.runtime_cleaned_at = None
            run.final_snapshot_completed_at = None
            run.claimed_by_runner = f"publication-recovery:{recovery_key}"
            run.claimed_at = utc_now()
            run.conversation.status = "execution_locked"
            if saved_writer_thread:
                repo.save_agent_thread(
                    scope_type="publication_replay",
                    scope_id=recovery_key,
                    role="paper_writer",
                    provider_thread_id=saved_writer_thread,
                    persistent=True,
                    model=model_profile["paper_writer"]["model"],
                    reasoning_effort=model_profile["paper_writer"]["reasoning_effort"],
                )
            for operation in session.scalars(
                select(AgentOperationCheckpointRecord).where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.attempt_id != attempt.attempt_id,
                    AgentOperationCheckpointRecord.status.in_(
                        ["pending", "running", "paused", "capacity_wait"]
                    ),
                )
            ):
                operation.status = "failed"
                operation.error = "Previous Attempt terminated before publication recovery"
                operation.updated_at = utc_now()
                operation.completed_at = utc_now()
            repo.append_event(
                "run",
                run_id,
                "publication_recovery_started",
                "已从冻结研究产物单独恢复论文写作；不会重新运行数学研究",
                {
                    "recovery_id": recovery_key,
                    "attempt_id": attempt.attempt_id,
                    "reused_writer_thread": bool(saved_writer_thread),
                    "reused_writer_v1": reuse_saved_v1,
                    "saved_v1_source": (
                        self.artifacts.reference(saved_v1_source)
                        if saved_v1_source is not None else None
                    ),
                    "publication_restarted_from_corpus": not reuse_saved_writer,
                },
            )
            attempt_id = attempt.attempt_id

        try:
            report_path = await self._write_final_report(
                user_id,
                run_id,
                attempt_id,
                "partially_resolved",
                model_profile_override=model_profile,
                output_root=self._run_root(run_id) / "final",
                publication_scope=recovery_key,
                record_run_phases=True,
                usage_component="prover",
                reuse_existing_v1=reuse_saved_v1,
            )
            await self._wait_public_summaries(run_id)
            source_path = report_path.with_suffix(".tex")
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                run = repo.get_run(user_id, run_id)
                attempt = session.get(RunAttemptRecord, attempt_id)
                if run.claimed_by_runner != f"publication-recovery:{recovery_key}":
                    raise DynamicProverError("出版恢复期间 Run claim 发生变化")
                run.status = "completed"
                run.completion_kind = "partially_resolved"
                run.completed_at = utc_now()
                run.terminal_at = run.completed_at
                run.final_report_path = None
                run.final_paper_pdf_path = self.artifacts.reference(report_path)
                run.final_paper_tex_path = self.artifacts.reference(source_path)
                run.pre_pause_status = None
                run.pre_pause_phase = None
                run.pause_reason = None
                run.pause_requested_at = None
                run.pause_requested_by = None
                run.paused_at = None
                run.resume_requested_at = None
                run.claimed_by_runner = None
                run.claimed_at = None
                if attempt is not None:
                    attempt.status = "completed"
                    attempt.error = None
                    attempt.completed_at = utc_now()
                run.conversation.status = "completed"
                run.conversation.completed_run_id = run_id
                actual = repo.settle_run_cost(run_id)
                repo.append_event(
                    "run",
                    run_id,
                    "run_completed",
                    "Research Run 完成：partially_resolved",
                    {"actual_cost": actual, "publication_recovery_id": recovery_key},
                )
            self.final_snapshots.export_run(run_id)
            return report_path
        except asyncio.CancelledError:
            self._mark_publication_recovery_interrupted(
                user_id, run_id, attempt_id, recovery_key, "出版恢复任务被取消"
            )
            raise
        except Exception as exc:
            self._mark_publication_recovery_failed(
                user_id, run_id, attempt_id, recovery_key, str(exc)
            )
            raise

    def _mark_publication_recovery_interrupted(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        recovery_id: str,
        reason: str,
    ) -> None:
        """保留可再次恢复的研究终态，并清除本次出版 claim。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            run.status = "interrupted"
            run.claimed_by_runner = None
            run.claimed_at = None
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt is not None:
                attempt.status = "interrupted"
                attempt.error = reason
                attempt.completed_at = utc_now()
            repo.append_event(
                "run", run_id, "publication_recovery_interrupted", reason,
                {"recovery_id": recovery_id},
            )

    def _mark_publication_recovery_failed(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        recovery_id: str,
        error: str,
    ) -> None:
        """记录出版技术失败，结算已用额度但不创建最终下载指针。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            run.status = "failed"
            run.completed_at = utc_now()
            run.terminal_at = run.completed_at
            run.claimed_by_runner = None
            run.claimed_at = None
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt is not None:
                attempt.status = "failed"
                attempt.error = error
                attempt.completed_at = utc_now()
            actual = repo.settle_run_cost(run_id)
            repo.append_event(
                "run", run_id, "publication_recovery_failed", error,
                {"recovery_id": recovery_id, "actual_cost": actual},
            )
        self.final_snapshots.export_run(run_id)

    @staticmethod
    def _publication_tree_digest(root: Path) -> str:
        """计算历史 final 的稳定树摘要，并忽略不会位于其中的 replay 目录。"""
        import hashlib

        digest = hashlib.sha256()
        if not root.is_dir():
            return digest.hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    async def revise_publication_replay_from_frozen_reviews(
        self,
        user_id: str,
        run_id: str,
        replay_id: str,
    ) -> Path:
        """仅重跑 Writer revision，把既有 Scout 报告按冻结证据过滤后生成 v3。"""
        with self.database.session() as session:
            run = RepositoryHub(session, self.artifacts).get_run(user_id, run_id)
            if run.status != "completed" or not run.attempts:
                raise DynamicProverError("只有已完成 Run 可以修订 publication replay")
            attempt_id = run.attempts[-1].attempt_id
            frozen = dict((run.execution_policy or {}).get("resolved_role_models") or {})
            model_profile = {
                "paper_writer": dict(frozen.get("paper_writer") or {}),
                "paper_auditor": dict(frozen.get("paper_auditor") or {}),
            }
        replay_root = self._run_root(run_id) / "runtime" / "publication-replays" / replay_id
        manifest_path = replay_root / "replay-manifest.json"
        publication_path = replay_root / "publication-manifest.json"
        v1_tex = replay_root / "paper-v001.tex"
        audit_path = replay_root / "paper-audit-v001.md"
        for path in (manifest_path, publication_path, v1_tex, audit_path):
            if not path.is_file():
                raise DynamicProverError(f"revision-only 缺少冻结产物：{path.name}")
        replay_manifest = dict(self.artifacts.read_json(self.artifacts.reference(manifest_path)))
        historical_final = self._run_root(run_id) / "final"
        historical_digest = self._publication_tree_digest(historical_final)
        if replay_manifest.get("historical_final_sha256") != historical_digest:
            raise DynamicProverError("revision-only 启动前历史 final 已变化")

        corpus = build_paper_source_corpus(
            self.database,
            self.artifacts,
            run_id,
            output_root=replay_root / "paper-source-corpus",
        )
        results: dict[str, dict[str, object]] = {}
        runtime_root = self._run_runtime_root(run_id)
        role_files = {
            "adversarial_prior_art_scout": ("report.md", "prior_art_report.md"),
            "related_work_context_scout": ("report.md", "related_work_report.md"),
        }
        for role, filenames in role_files.items():
            scout_root = replay_root / "scouts" / role
            original_report = scout_root / "report.md"
            workspace = runtime_root / "agents" / f"publication_scout-{replay_id}-{role}-{role}" / role
            if not original_report.is_file():
                candidates = [workspace / name for name in filenames if (workspace / name).is_file()]
                if not candidates:
                    results[role] = {"role": role, "status": "failed", "source_count": 0}
                    continue
                original_report = max(candidates, key=lambda path: path.stat().st_mtime_ns)
            archive = scout_root / "literature"
            if not (archive / "evidence_manifest.json").is_file():
                manifests = sorted(workspace.glob("operations/*/literature/evidence_manifest.json"))
                if manifests:
                    archive.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(manifests[-1].parent, archive, dirs_exist_ok=True)
            evidence_manifest = archive / "evidence_manifest.json"
            if not evidence_manifest.is_file():
                results[role] = {"role": role, "status": "failed", "source_count": 0}
                continue
            payload = dict(self.artifacts.read_json(self.artifacts.reference(evidence_manifest)))
            evidence = {
                "kind": "literature",
                "operation_root": str(scout_root),
                **dict(payload.get("summary") or {}),
            }
            filtered, count, removed = filter_scout_report_to_frozen_evidence(
                original_report.read_text(encoding="utf-8"), evidence
            )
            filtered_path = scout_root / "writer-validated-report.md"
            self.artifacts.write_text(self.artifacts.reference(filtered_path), filtered)
            results[role] = {
                "role": role,
                "status": "completed",
                "report_path": self.artifacts.reference(filtered_path),
                "literature_path": self.artifacts.reference(archive),
                "source_count": count,
                "removed_unverified_sources": removed,
            }
        revision_inputs = self._publication_revision_inputs(
            replay_root, v1_tex, audit_path, results
        )
        genuine_count = corpus.genuine_source_count + sum(
            int(item.get("source_count") or 0) for item in results.values()
        )
        revised_build: LatexCompilation | None = None

        def parser(text: str) -> tuple[str | None, ValidationResult]:
            nonlocal revised_build
            build, errors = self.latex_compiler.compile(
                text,
                require_theorem=corpus.verified_result_count > 0,
                genuine_source_count=genuine_count,
            )
            if errors or build is None:
                return None, ValidationResult(errors, [], format_name="LaTeX")
            revised_build = build
            return text, ValidationResult([], [], format_name="LaTeX")

        task = """# Revise paper-v001.tex using the frozen validated literature reviews

Read `paper-v001.tex`, `paper-audit-v001.md`, and both available Scout directories.
The files named `report.md` are Controller-filtered Writer copies: only the arXiv IDs
listed under `Controller-validated citation boundary` may enter the bibliography.
Integrate those sources naturally into the Introduction, Related Work, and Discussion.
Do not mention Scouts, evidence levels, Controller filtering, local paths, or MathLab.
Return only one complete raw English LaTeX document. Preserve
the manuscript's verified mathematical claims; do not create a new theorem.
"""
        revised, _ = await self._validated_role(
            user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=None,
            role="paper_writer", role_key="paper_writer",
            scope_type="publication_replay", scope_id=replay_id,
            task=task, parser=parser, persistent=True,
            context_paths=[*corpus.writer_context_paths, revision_inputs],
            model_profile_override=model_profile, output_format="latex",
            usage_component="publication_replay_literature_fix",
        )
        if revised_build is None:
            raise DynamicProverError("revision-only Writer 通过但缺少编译结果")
        v3_tex, v3_pdf, _ = self._persist_latex_compilation(
            replay_root, "paper-v003", revised_build
        )
        self.artifacts.write_text(self.artifacts.reference(replay_root / "paper.tex"), revised)
        self.artifacts.write_bytes(
            self.artifacts.reference(replay_root / "paper.pdf"), revised_build.pdf_bytes
        )
        publication = dict(self.artifacts.read_json(self.artifacts.reference(publication_path)))
        publication["literature_boundary_repair"] = {
            "reviews": results,
            "revision": {
                "tex_path": self.artifacts.reference(v3_tex),
                "pdf_path": self.artifacts.reference(v3_pdf),
                "tex_sha256": revised_build.source_sha256,
                "pdf_sha256": revised_build.pdf_sha256,
                "pages": revised_build.page_count,
            },
        }
        publication["published_tex_path"] = self.artifacts.reference(replay_root / "paper.tex")
        publication["published_pdf_path"] = self.artifacts.reference(replay_root / "paper.pdf")
        self.artifacts.write_json(self.artifacts.reference(publication_path), publication)
        replay_manifest.update(
            {
                "literature_boundary_repaired_at": utc_now().isoformat(),
                "paper_tex_path": self.artifacts.reference(replay_root / "paper.tex"),
                "paper_pdf_path": self.artifacts.reference(replay_root / "paper.pdf"),
                "historical_final_unchanged": self._publication_tree_digest(historical_final)
                == historical_digest,
            }
        )
        self.artifacts.write_json(self.artifacts.reference(manifest_path), replay_manifest)
        return replay_root / "paper.pdf"

    def _delete_legacy_publication_markdown(self, run_id: str) -> list[str]:
        """永久删除旧报告链的生成稿；保留 instructions、stream 和 usage 审计。"""
        targets = [
            self._run_root(run_id) / "final" / "research-report-v001.md",
            self._run_root(run_id) / "final" / "audit-v001.md",
            self._run_root(run_id) / "final" / "research-report.md",
            self._run_root(run_id) / "final" / "paper.md",
        ]
        targets.extend(self._run_root(run_id).glob("final/paper-v[0-9][0-9][0-9].md"))
        runtime = self._run_runtime_root(run_id) / "agents"
        roots = [
            path for path in runtime.glob("*")
            if path.is_dir() and ("report_writer" in path.name or "report_auditor" in path.name)
        ]
        for root in roots:
            targets.extend(root.glob("*/report.md"))
            if root.is_dir():
                targets.extend(root.glob("*/turns/*/repair-*/final_message.md"))
        deleted: list[str] = []
        for path in targets:
            if path.is_file():
                path.unlink()
                deleted.append(str(path.relative_to(self.artifacts.data_root)))
        return sorted(deleted)

    # ------------------------------------------------------------------ shared agent validation
    @staticmethod
    def _elapsed_seconds(started: Any) -> float:
        """兼容 PostgreSQL 时区时间与 SQLite 测试中的 naive UTC 时间计算墙钟。"""
        now = utc_now()
        if getattr(started, "tzinfo", None) is None:
            now = now.replace(tzinfo=None)
        return float((now - started).total_seconds())

    def _run_research_elapsed_seconds(self, run_id: str, started: Any) -> float:
        """返回扣除全部暂停区间后的真实研究墙钟。"""
        elapsed = self._elapsed_seconds(started)
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return elapsed
            paused = float(run.total_paused_seconds or 0)
            if run.status == "paused" and run.paused_at is not None:
                paused += self._elapsed_seconds(run.paused_at)
        return max(0.0, elapsed - paused)

    def _run_is_paused(self, run_id: str) -> bool:
        """判断取消是否来自用户暂停，而非删除、失败或服务收束。"""
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            return bool(run is not None and run.status == "paused")

    def _run_duration_seconds(self, run: ResearchRunRecord) -> int:
        """读取 Run 冻结的墙钟预算；在线旧 Run 缺字段时在付费调用前拒绝。"""
        duration = int((run.execution_policy or {}).get("duration_seconds") or 0)
        if duration > 0:
            return duration
        if self.settings.offline_agents:
            return int(self.settings.default_run_seconds)
        raise DynamicProverError("Research Run 缺少有效的 frozen duration_seconds")

    async def _validated_role(self, *, user_id: str, run_id: str, attempt_id: str, node_id: str | None, role: str, role_key: str, scope_type: str, scope_id: str, task: str, parser: Parser[Parsed], persistent: bool, network_access: bool = False, literature_access: bool = False, literature_limits: LiteratureLimits | None = None, literature_archive_root: Path | None = None, context_paths: list[Path] | None = None, reviewer_evidence_artifacts: list[dict[str, object]] | None = None, model_profile_override: dict[str, dict[str, str]] | None = None, output_format: str = "markdown", usage_component: str = "prover", turn_validator: Callable[[Parsed | None, AgentTurn, str], ValidationResult] | None = None, operation_kind: str | None = None, checkpoint_payload: dict[str, Any] | None = None) -> tuple[Parsed, AgentTurn]:
        """执行一次版本化角色操作，并在同一证据目录内完成有限格式修复。"""
        model_profile: dict[str, Any] = {}
        research_language = "zh"
        operation_id: str
        original_task = task
        checkpoint_enabled = False
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            if not self.settings.offline_agents:
                run = repo.get_run(user_id, run_id)
                model_profile = dict(
                    model_profile_override
                    or (run.execution_policy or {}).get("resolved_role_models")
                    or {}
                )
                if not model_profile:
                    raise DynamicProverError("Research Run 缺少 frozen resolved_role_models")
            thread = repo.get_agent_thread(scope_type, scope_id, role_key)
            provider_thread_id = thread.provider_thread_id if thread else None
            run_record = session.get(ResearchRunRecord, run_id)
            if run_record is not None:
                research_language = str(
                    getattr(run_record, "research_language", "zh") or "zh"
                )
            attempt_record = session.get(RunAttemptRecord, attempt_id)
            checkpoint_enabled = bool(
                run_record is not None
                and attempt_record is not None
                and attempt_record.run_id == run_id
            )
            paused_operation = session.scalar(
                select(AgentOperationCheckpointRecord)
                .where(
                    AgentOperationCheckpointRecord.run_id == run_id,
                    AgentOperationCheckpointRecord.attempt_id == attempt_id,
                    AgentOperationCheckpointRecord.role_key == role_key,
                    AgentOperationCheckpointRecord.scope_type == scope_type,
                    AgentOperationCheckpointRecord.scope_id == scope_id,
                    AgentOperationCheckpointRecord.status == "paused",
                    AgentOperationCheckpointRecord.pause_epoch == run_record.pause_epoch,
                )
                .order_by(AgentOperationCheckpointRecord.created_at.desc())
                .limit(1)
            ) if checkpoint_enabled else None
            if paused_operation is not None:
                operation_id = paused_operation.operation_id
                provider_thread_id = paused_operation.provider_thread_id or provider_thread_id
                original_task = self.artifacts.read_text(paused_operation.task_path)
            else:
                operation_id = new_id("operation")
                task_path = self._run_runtime_root(run_id) / "agents" / "_operations" / operation_id / "task.md"
                self.artifacts.write_text(task_path.relative_to(self.artifacts.data_root), original_task)
                if checkpoint_enabled:
                    repo.begin_agent_operation(
                        operation_id=operation_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        node_id=node_id,
                        operation_kind=operation_kind or role,
                        role=role,
                        role_key=role_key,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        task_path=task_path,
                        payload=checkpoint_payload,
                        pause_epoch=int(run_record.pause_epoch if run_record is not None else 0),
                    )
        current = task
        last_turn: AgentTurn | None = None
        # Fresh means a new identity for this business operation.  The transport must
        # nevertheless retain that identity until its bounded schema-repair
        # transaction finishes; it is deliberately not saved to agent_threads below.
        transport_persistent = persistent or self.settings.contract_repair_attempts > 0
        repair = 0
        transport_retry = 0
        writer_deadline_extended = False
        writer_warning_recorded = False
        while (
            repair <= self.settings.contract_repair_attempts
            or (role == "paper_writer" and writer_deadline_extended)
        ):
            await self._await_capacity_hold(run_id)
            with self.database.session() as session:
                run_state = session.get(ResearchRunRecord, run_id)
                if run_state is not None and (
                    run_state.pause_requested_at is not None
                    or run_state.status in {"paused", "resuming"}
                ):
                    if checkpoint_enabled:
                        operation = session.get(
                            AgentOperationCheckpointRecord, operation_id
                        )
                        if operation is not None:
                            operation.status = "paused"
                            operation.pause_epoch = run_state.pause_epoch
                            operation.updated_at = utc_now()
                    if run_state.status != "paused":
                        await self._enter_run_pause(
                            run_id, reason=run_state.pause_reason or "user"
                        )
                    await self._wait_until_resumed(run_id)
            if checkpoint_enabled:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).set_agent_operation_status(
                        operation_id, "running", repair_number=repair
                    )

            async def on_thread_ready(real_thread_id: str) -> None:
                nonlocal provider_thread_id
                provider_thread_id = real_thread_id
                if not checkpoint_enabled:
                    return
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    repo.update_agent_operation_thread(operation_id, real_thread_id)

            try:
                async with self._run_semaphore(run_id):
                    turn = await self.backend.run_role(
                        role=role, scope_id=f"{scope_type}-{scope_id}-{role_key.replace(':', '-')}",
                        task=current, persistent=transport_persistent, provider_thread_id=provider_thread_id,
                        repair_number=repair, operation_id=operation_id,
                        network_access=network_access,
                        literature_access=literature_access,
                        literature_limits=literature_limits,
                        literature_archive_root=literature_archive_root,
                        context_paths=context_paths,
                        reviewer_evidence_artifacts=reviewer_evidence_artifacts,
                        model_profile=model_profile or None,
                        runtime_root=self._run_runtime_root(run_id),
                        on_thread_ready=on_thread_ready,
                        writer_deadline_mode=(
                            "unlimited" if writer_deadline_extended else "bounded"
                        ),
                        output_language=research_language,
                    )
            except Exception as exc:
                if (
                    transport_retry < 1
                    and self._is_retryable_agent_transport_error(str(exc))
                ):
                    transport_retry += 1
                    current = (
                        "Continue the same operation after the transient transport timeout. "
                        "Do not restart the research or change the assignment. Submit the complete "
                        "original deliverable now."
                        if provider_thread_id
                        else original_task
                    )
                    if checkpoint_enabled:
                        with self.database.session() as session:
                            RepositoryHub(session, self.artifacts).append_event(
                                "run",
                                run_id,
                                "agent_transport_retry",
                                f"{role_key} 的上游连接超时；复用同一 operation/thread 重试一次",
                                {
                                    "operation_id": operation_id,
                                    "role_key": role_key,
                                    "provider_thread_id": provider_thread_id,
                                    "error": str(exc),
                                },
                            )
                    continue
                raise
            last_turn = turn
            provider_thread_id = turn.thread_id or provider_thread_id
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                repo.record_usage(
                    user_id=user_id, run_id=run_id, attempt_id=attempt_id, node_id=node_id,
                    component=usage_component, role=role_key, model=turn.model, usage=turn.usage,
                    provider_thread_id=turn.thread_id,
                    usage_semantics=turn.usage_semantics,
                    cost_estimator=lambda normalized: self.settings.estimate_turn_cost(
                        turn.model, normalized
                    ),
                )
                if persistent and provider_thread_id:
                    repo.save_agent_thread(
                        scope_type=scope_type, scope_id=scope_id, role=role_key,
                        provider_thread_id=provider_thread_id, persistent=True,
                        model=turn.model, reasoning_effort=turn.reasoning_effort,
                    )
                    if scope_type == "worker":
                        worker = session.get(WorkerIdentityRecord, scope_id)
                        if worker is not None:
                            worker.provider_thread_id = provider_thread_id
                if checkpoint_enabled and provider_thread_id:
                    repo.update_agent_operation_thread(operation_id, provider_thread_id)
                if checkpoint_enabled:
                    repo.set_agent_operation_status(
                        operation_id,
                        "paused" if turn.status == "paused" else "running",
                        repair_number=repair,
                        error=turn.error,
                    )
                deadline_evidence = dict(turn.evidence or {})
                if (
                    role == "paper_writer"
                    and deadline_evidence.get("deadline_warning_sent")
                    and not writer_warning_recorded
                ):
                    repo.append_event(
                        "run", run_id, "paper_writer_deadline_warning",
                        "Writer 剩余十分钟，已中断并复用同一 thread 发送收尾提醒",
                        {"operation_id": operation_id, "role_key": role_key},
                    )
                    writer_warning_recorded = True
            if turn.status == "paused":
                await self._wait_until_resumed(run_id)
                current = (
                    "Continue the same interrupted operation. Do not start a new assignment. "
                    "Complete and submit the exact deliverable requested before the pause."
                    if provider_thread_id
                    else original_task
                )
                continue
            if turn.status not in {"completed", "deadline_expired"}:
                if self._is_explicit_capacity_error(turn.error):
                    await self._handle_capacity_failure(
                        run_id, operation_id, role_key, str(turn.error or "429")
                    )
                    current = (
                        "Continue the same operation after the provider-capacity pause. "
                        "Submit the complete original deliverable."
                        if provider_thread_id
                        else original_task
                    )
                    continue
                label = "LaTeX" if output_format == "latex" else "Markdown"
                current = (
                    f"上一轮调用未正常完成：{turn.error or turn.status}\n请重新提交完整业务 {label}。"
                    if provider_thread_id
                    else original_task
                )
                repair += 1
                continue
            normalized = (
                normalize_latex_output(turn.text)
                if output_format == "latex"
                else self._normalize_markdown(turn.text)
            )
            parsed, validation = parser(normalized)
            if turn_validator is not None:
                extra = turn_validator(parsed, turn, normalized)
                validation = ValidationResult(
                    list(dict.fromkeys([*validation.errors, *extra.errors])),
                    list(dict.fromkeys([*validation.warnings, *extra.warnings])),
                    format_name=validation.format_name,
                )
            if validation.ok and parsed is not None:
                if literature_access and not self.settings.offline_agents:
                    self._validate_literature_evidence(turn)
                if checkpoint_enabled:
                    with self.database.session() as session:
                        repo = RepositoryHub(session, self.artifacts)
                        repo.set_agent_operation_status(
                            operation_id, "completed", repair_number=repair
                        )
                        if turn.status == "deadline_expired":
                            repo.append_event(
                                "run", run_id, "paper_writer_deadline_artifact_accepted",
                                "Writer 未在四十分钟内正常结束，Controller 已接受其最新可编译稿",
                                {"operation_id": operation_id, "role_key": role_key},
                            )
                return parsed, turn
            if turn.status == "deadline_expired" and role == "paper_writer":
                writer_deadline_extended = True
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).append_event(
                        "run", run_id, "paper_writer_deadline_extended",
                        "Writer 四十分钟时的最新稿未通过编译门禁，已复用同一 thread 继续修复，不再设硬截止",
                        {
                            "operation_id": operation_id,
                            "role_key": role_key,
                            "errors": list(validation.errors),
                        },
                    )
            current = validation.repair_message()
            repair += 1
        error = f"角色 {role_key} 在 {self.settings.contract_repair_attempts} 次 repair 后仍未提交有效结果：{last_turn.error if last_turn and last_turn.error else 'schema invalid'}"
        if checkpoint_enabled:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).set_agent_operation_status(
                    operation_id, "failed", error=error
                )
        last_markdown = ""
        if last_turn is not None and last_turn.text:
            last_markdown = (
                normalize_latex_output(last_turn.text)
                if output_format == "latex"
                else self._normalize_markdown(last_turn.text)
            )
        raise RoleContractError(role_key, error, last_markdown)

    @staticmethod
    def _is_explicit_capacity_error(error: str | None) -> bool:
        """只识别明确 Provider 容量错误，不把普通网络故障或 5xx 当成 429。"""
        text = str(error or "").lower()
        return bool(re.search(r"\b429\b", text)) or any(marker in text for marker in (
            "too many requests", "rate limit", "rate_limit", "concurrency limit",
        ))

    @classmethod
    def _is_retryable_agent_transport_error(cls, error: str | None) -> bool:
        """只为一次性上游断流/模型调用超时复用原 operation，不吞掉业务或工具错误。"""
        text = str(error or "").lower()
        if cls._is_explicit_capacity_error(text):
            return False
        return any(marker in text for marker in (
            "模型调用超过",
            "upstream request failed",
            "stream disconnected before completion",
            "connection reset by peer",
            "connection closed before message completed",
        ))

    async def _await_capacity_hold(self, run_id: str) -> None:
        """首个容量事故后短暂停发新调用，同时允许已在运行的调用返回。"""
        while True:
            deadline = self._capacity_holds.get(run_id)
            if deadline is None:
                return
            now = utc_now()
            if now >= deadline:
                self._capacity_holds.pop(run_id, None)
                return
            with self.database.session() as session:
                run = session.get(ResearchRunRecord, run_id)
                status = run.status if run is not None else "failed"
            if status in {"paused", "resuming"}:
                await self._wait_until_resumed(run_id)
                self._capacity_holds.pop(run_id, None)
                return
            await asyncio.sleep(min(0.5, max(0.05, (deadline - now).total_seconds())))

    async def _handle_capacity_failure(
        self, run_id: str, operation_id: str, role_key: str, error: str
    ) -> None:
        """记录明确 429；两项不同业务调用或同项二次失败即安全暂停全 Run。"""
        lock = self._capacity_locks.setdefault(run_id, asyncio.Lock())
        should_pause = False
        async with lock:
            now = utc_now()
            cutoff = now - timedelta(seconds=60)
            incidents = [
                item for item in self._capacity_incidents.get(run_id, [])
                if item[0] >= cutoff
            ]
            incidents.append((now, operation_id))
            self._capacity_incidents[run_id] = incidents
            distinct = {item[1] for item in incidents}
            same_count = sum(item[1] == operation_id for item in incidents)
            should_pause = len(distinct) >= 2 or same_count >= 2
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                repo.set_agent_operation_status(
                    operation_id, "capacity_wait", error=error
                )
                repo.append_event(
                    "run", run_id, "provider_capacity_failure",
                    "模型调用触发明确 Provider 容量限制",
                    {
                        "operation_id": operation_id,
                        "role": role_key,
                        "error": error,
                        "window_seconds": 60,
                        "distinct_operations": len(distinct),
                    },
                )
                run = session.get(ResearchRunRecord, run_id)
                if should_pause and run is not None and run.status in {"running", "draining"}:
                    run.pause_requested_at = now
                    run.pause_reason = "provider_capacity"
                    run.pause_epoch += 1
            if not should_pause:
                self._capacity_holds[run_id] = now + timedelta(seconds=60)
        if should_pause:
            await self._enter_run_pause(run_id, reason="provider_capacity")
            await self._wait_until_resumed(run_id)
            self._capacity_incidents.pop(run_id, None)
            self._capacity_holds.pop(run_id, None)
        else:
            await self._await_capacity_hold(run_id)

    @staticmethod
    def _validate_literature_evidence(turn: AgentTurn) -> None:
        """文献报告可无检索参加组会；真实物化论文时才核验冻结证据。"""
        evidence = turn.evidence or {}
        if not evidence or evidence.get("kind") != "literature":
            return
        papers = list(evidence.get("papers") or [])
        if papers:
            for paper in papers:
                item = dict(paper)
                if not item.get("pdf_sha256") and not item.get("source_sha256"):
                    raise DynamicProverError("已选择论文但未冻结 TeX source/PDF 与 SHA256")
            if int(evidence.get("reading_evidence_count") or 0) < len(papers):
                raise DynamicProverError("已下载论文但未生成分层阅读证据")

    def _publish_literature_evidence(
        self,
        run_id: str,
        node_id: str,
        worker_id: str,
        assignment_version: int,
        evidence: dict[str, object],
    ) -> Path:
        """把去敏检索、TeX/PDF、阅读证据复制到 Run 公共证据，不发布 token 或 transport。"""
        operation_root = Path(str(evidence.get("operation_root") or "")).resolve()
        source = operation_root / "literature"
        runtime_root = self._run_runtime_root(run_id).resolve()
        if not source.is_dir() or not source.is_relative_to(runtime_root):
            raise DynamicProverError("文献证据源不在 MathLab 管理的 runtime 内")
        target = (
            self._node_root(run_id, node_id)
            / "literature"
            / worker_id
            / f"assignment-v{assignment_version:03d}"
        )
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
        papers = [dict(item) for item in list(evidence.get("papers") or [])]
        if not papers:
            message = "定理与摘要检索证据已冻结；本轮未选择论文原文"
        elif any(int(item.get("tex_file_count") or 0) > 0 for item in papers):
            message = "所选论文的 TeX source、PDF 与分层阅读证据已冻结"
        else:
            message = "所选论文的 TeX source 不可用；PDF 与分层阅读证据已冻结"
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).append_event(
                "node", node_id, "literature_evidence_published",
                message,
                {"worker_id": worker_id, "assignment_version": assignment_version, "path": self.artifacts.reference(target)},
            )
        return target

    async def _verify_candidate_safely(
        self,
        user_id: str,
        run_id: str,
        attempt_id: str,
        node_id: str,
        candidate_id: str,
        candidate_path: Path,
    ) -> tuple[VerificationDecision | None, str, bool]:
        """按精确命题串行核验，避免同 Node 的修订版 Candidate 重复入库。"""
        try:
            metadata = self._candidate_metadata(node_id, candidate_id, candidate_path)
            statement_sha256 = str(metadata.get("statement_sha256") or "")
        except Exception:
            statement_sha256 = ""
        key = (run_id, statement_sha256 or candidate_id)
        lock = self._verification_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                decision, newly_verified = await self._verify_candidate_path(
                    user_id, run_id, attempt_id, node_id, candidate_id, candidate_path
                )
                return decision, "", newly_verified
            except Exception as exc:
                error = str(exc)
                with self.database.session() as session:
                    repo = RepositoryHub(session, self.artifacts)
                    node = session.get(ResearchNodeRecord, node_id)
                    if node is not None:
                        details = dict(node.details or {})
                        failures = list(details.get("verification_failures") or [])
                        failures.append({"candidate_artifact_id": candidate_id, "error": error})
                        details["verification_failures"] = failures[-20:]
                        node.details = details
                    repo.append_event(
                        "node",
                        node_id,
                        "claim_verification_unavailable",
                        "Fresh Claim Verifier 本次未形成数学裁决",
                        {"candidate_artifact_id": candidate_id, "error": error},
                    )
                self._record_verification_feedback(
                    run_id,
                    node_id,
                    candidate_id,
                    verdict="UNAVAILABLE",
                    markdown="",
                    error=error,
                )
                return None, error, False

    async def _cancel_run_children(self, run_id: str) -> None:
        """在 Run 技术失败时取消并回收所有 Node、Worker 和 Architecture 子任务。"""
        current = asyncio.current_task()
        tasks: set[asyncio.Task[Any]] = {
            task
            for task in self._node_tasks.get(run_id, {}).values()
            if task is not current and not task.done()
        }
        for node_id in self._node_tasks.get(run_id, {}):
            tasks.update(
                task
                for task in self._worker_tasks.get(node_id, {}).values()
                if task is not current and not task.done()
            )
        architecture = self._architecture_tasks.get(run_id)
        if architecture is not None and architecture is not current and not architecture.done():
            tasks.add(architecture)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ context helpers
    def _materialize_initial_nodes(self, user_id: str, run_id: str) -> None:
        created_node_ids: list[str] = []
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if repo.list_nodes(run_id):
                return
            handoff = self.artifacts.read_json(run.handoff_manifest_path)
            for item in handoff.get("initial_nodes", []):
                node = repo.create_node(
                    run, kind=str(item.get("kind") or "discovery"),
                    title=str(item.get("title") or item.get("label") or "Initial Research Node"),
                    objective=str(item.get("objective") or ""),
                    completion_criterion=str(item.get("completion_criterion") or ""),
                    details={
                        "planner_label": item.get("label"),
                        "raw_markdown": item.get("raw_markdown"),
                        "original_contract_uncertainty_reduced": item.get(
                            "original_contract_uncertainty_reduced"
                        ),
                    },
                    status="ready",
                )
                if node.kind == "claim":
                    node_details = dict(node.details or {})
                    node_details["controlling_proposition_id"] = self._controlling_proposition_id(node.node_id)
                    node.details = node_details
                created_node_ids.append(node.node_id)
            if created_node_ids:
                repo.add_global_state_delta(
                    run,
                    change_kind="initial_nodes_materialized",
                    new_summary={"node_ids": created_node_ids},
                    evidence_refs=created_node_ids,
                    root_relevant=False,
                )
            repo.append_event("run", run_id, "initial_nodes_materialized", "Detailed Research Nodes 已进入 ready queue")
        for node_id in created_node_ids:
            self._queue_node_public_card(node_id)

    def _node_context(self, user_id: str, run_id: str, node_id: str) -> tuple[Any, str, str, str, str, str, str, str]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            node = session.get(ResearchNodeRecord, node_id)
            if node is None or node.run_id != run_id:
                raise DynamicProverError("Research Node 不存在")
            contract = self.artifacts.read_text(run.contract_snapshot_path)
            claims = self._claims_markdown(repo.list_verified_claims(run_id))
            workers = repo.list_workers(node_id)
            worker_state_parts = []
            for worker in workers:
                current = repo.current_assignment(worker.worker_id)
                worker_state_parts.append(
                    f"### {worker.worker_label}\nWorker ID: {worker.worker_id}\nPersona: {worker.persona}\nStatus: {worker.status}\nThread: {'persistent' if worker.provider_thread_id else 'not-yet-created'}\nCurrent Assignment: {current.task_markdown if current else '无'}"
                )
            details = node.details or {}
            directives = "\n".join(details.get("architecture_directives", []))
            notes = str(details.get("supervisor_notes") or "")
            edges = list(session.scalars(select(ResearchGraphEdgeRecord).where(
                ResearchGraphEdgeRecord.run_id == run_id,
                (ResearchGraphEdgeRecord.source_node_id == node_id)
                | (ResearchGraphEdgeRecord.target_node_id == node_id),
            )))
            relation_lines: list[str] = []
            if node.parent_node_id:
                parent = session.get(ResearchNodeRecord, node.parent_node_id)
                relation_lines.append(
                    f"Parent: {parent.node_id} · {parent.title}" if parent else f"Parent: {node.parent_node_id}"
                )
            for edge in edges:
                relation_lines.append(
                    f"- {edge.source_node_id} --{edge.relation}--> {edge.target_node_id}"
                    + (f" · Condition: {edge.condition}" if edge.condition else "")
                )
            started = run.started_at or run.created_at
            elapsed = self._run_research_elapsed_seconds(run_id, started)
            duration = self._run_duration_seconds(run)
            review_count = max(
                int(node.local_review_count or 0),
                max(0, self._count_supervisor_artifacts(node_id) - 1),
            )
            resources = "\n".join([
                f"Research elapsed seconds: {int(elapsed)}",
                f"Research remaining seconds: {max(0, int(duration - elapsed))}",
                f"Local reviews used: {review_count}/{self.settings.max_local_reviews_per_node}",
                f"Worker cap: {self.settings.max_workers_per_research_node}",
                f"Global active-node cap: {self.settings.max_parallel_research_nodes}",
            ])
            return (
                self._detach_node(node), contract, claims, "\n\n".join(worker_state_parts),
                directives, notes, "\n".join(relation_lines), resources,
            )

    def _worker_context(self, user_id: str, run_id: str, node_id: str, worker_id: str, assignment_id: str) -> tuple[Any, Any, Any, str, str, list[Path]]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            node = session.get(ResearchNodeRecord, node_id)
            worker = session.get(WorkerIdentityRecord, worker_id)
            assignment = session.get(WorkerAssignmentRecord, assignment_id)
            if not node or not worker or not assignment or assignment.worker_id != worker_id or worker.node_id != node_id:
                raise DynamicProverError("Worker/Assignment 不存在或归属不一致")
            contract = self.artifacts.read_text(run.contract_snapshot_path)
            claims = self._claims_markdown(repo.list_verified_claims(run_id))
            source_root = self.artifacts.resolve(run.source_snapshot_path)
            sources = [path for path in source_root.iterdir() if path.is_file()] if source_root.is_dir() else []
            return self._detach_assignment(assignment), self._detach_worker(worker), self._detach_node(node), contract, claims, sources

    def _refresh_architecture_snapshot(
        self,
        run_id: str,
        *,
        root_progress: bool = False,
        reviewer_patch: str | None = None,
        last_review_research_seconds: float | None = None,
    ) -> None:
        """从 canonical DB 重建当前白板；Reviewer patch 只补充全局判断，不覆盖事实字段。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return
            existing = session.get(ArchitectureSnapshotRecord, run_id)
            previous_payload = dict(existing.payload or {}) if existing is not None else {}
            nodes = repo.list_nodes(run_id)
            edges = list(session.scalars(select(ResearchGraphEdgeRecord).where(
                ResearchGraphEdgeRecord.run_id == run_id
            ).order_by(ResearchGraphEdgeRecord.created_at)))
            claims = repo.list_verified_claims(run_id)
            claims_by_node: dict[str, list[str]] = {}
            for claim in claims:
                claims_by_node.setdefault(claim.node_id, []).append(claim.verified_claim_id)
            node_payloads: list[dict[str, Any]] = []
            for node in nodes:
                details = dict(node.details or {})
                missing_bridge = session.scalar(
                    select(ResearchFindingRecord).where(
                        ResearchFindingRecord.node_id == node.node_id,
                        ResearchFindingRecord.finding_type == "missing_bridge",
                    ).order_by(ResearchFindingRecord.created_at.desc()).limit(1)
                )
                relations = [
                    {
                        "source": edge.source_node_id,
                        "target": edge.target_node_id,
                        "relation": edge.relation,
                        "condition": edge.condition,
                    }
                    for edge in edges
                    if edge.source_node_id == node.node_id or edge.target_node_id == node.node_id
                ]
                candidate_id = str(details.get("latest_candidate_artifact_id") or "")
                diagnosis = str(details.get("latest_supervisor_diagnosis") or "")
                node_payloads.append({
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "title": node.title,
                    "status": node.status,
                    "node_version": node.node_version,
                    "objective": node.objective,
                    "completion_criterion": node.completion_criterion,
                    "original_contract_uncertainty_reduced": str(
                        details.get("original_contract_uncertainty_reduced") or ""
                    ),
                    "strongest_current_result": str(
                        details.get("strongest_current_result") or "尚无正式可复用结果。"
                    ),
                    "minimal_sufficient_target": str(
                        details.get("minimal_sufficient_target") or node.completion_criterion
                    ),
                    "decisive_gap": str(
                        details.get("decisive_gap")
                        or (missing_bridge.markdown if missing_bridge else "")
                    ),
                    "gap_key": (
                        re.sub(r"\s+", " ", missing_bridge.title.strip().lower())[:300]
                        if missing_bridge else ""
                    ),
                    "verified_claim_ids": claims_by_node.get(node.node_id, []),
                    "candidate_artifact_id": candidate_id or None,
                    "why_still_active": (
                        str(details.get("why_node_is_still_active") or "")
                        if node.status in {"ready", "active", "draining"}
                        else f"Node 当前为 {node.status}。"
                    ),
                    "critical_path_relations": relations,
                    "final_local_stage": node.final_stage_path,
                })
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "graph_version": run.graph_version,
                "nodes": node_payloads,
                "edges": [
                    {
                        "source": edge.source_node_id,
                        "target": edge.target_node_id,
                        "relation": edge.relation,
                        "condition": edge.condition,
                    }
                    for edge in edges
                ],
                "reviewer_snapshot_update": (
                    reviewer_patch if reviewer_patch is not None and reviewer_patch.strip().lower() not in {
                        "", "无", "无。", "暂无", "暂无。", "无需更新", "无需更新。", "none"
                    }
                    else str(previous_payload.get("reviewer_snapshot_update") or "")
                ),
                "last_root_progress_research_seconds": (
                    self._run_research_elapsed_seconds(run_id, run.started_at or run.created_at)
                    if root_progress
                    else float(previous_payload.get("last_root_progress_research_seconds", 0))
                ),
            }
            blocks = ["# Current Architecture Snapshot", f"Graph Version: {run.graph_version}"]
            patch_text = str(payload["reviewer_snapshot_update"] or "")
            if patch_text:
                blocks.extend(["## Last Fresh Reviewer Global Patch", patch_text])
            blocks.append("## Current Nodes")
            for item in node_payloads:
                blocks.append("\n".join([
                    f"### {item['node_id']} · {item['title']}",
                    f"Kind: {item['kind']}",
                    f"Status: {item['status']}",
                    f"Node Version: {item['node_version']}",
                    f"Objective: {item['objective']}",
                    f"Completion Criterion: {item['completion_criterion']}",
                    f"Original-Contract Uncertainty Reduced: {item['original_contract_uncertainty_reduced']}",
                    f"Strongest Current Result: {item['strongest_current_result']}",
                    f"Minimal Sufficient Target: {item['minimal_sufficient_target']}",
                    f"Decisive Gap: {item['decisive_gap'] or '尚未形成压缩 gap。'}",
                    f"Verified/Candidate Boundary: verified={','.join(item['verified_claim_ids']) or '无'}; candidate={item['candidate_artifact_id'] or '无'}",
                    f"Why Still Active: {item['why_still_active']}",
                    f"Final/Latest Local Stage: {item['final_local_stage'] or '无'}",
                    "Critical-path Relations: " + (json.dumps(item["critical_path_relations"], ensure_ascii=False) or "[]"),
                ]))
            path = self._run_root(run_id) / "runtime" / "architecture" / "current-snapshot.md"
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), "\n\n".join(blocks) + "\n")
            repo.upsert_architecture_snapshot(
                run,
                payload=payload,
                snapshot_path=path,
                last_review_research_seconds=last_review_research_seconds,
                root_progress=root_progress,
            )

    def _architecture_snapshot(self, run_id: str, review_id: str) -> dict[str, Any]:
        """冻结一次 fresh Reviewer 的压缩输入和可按需读取的 Artifact 白名单。"""
        self._refresh_architecture_snapshot(run_id)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            snapshot = session.get(ArchitectureSnapshotRecord, run_id)
            if run is None or snapshot is None:
                raise DynamicProverError("Research Run Architecture Snapshot 不存在")
            deltas = repo.pending_global_state_deltas(run_id)
            communications = repo.pending_architecture_communications(run_id)
            ledger = repo.list_architecture_ledger(run_id)
            promotions = repo.pending_promotions(run_id)
            promotion_findings = {item.proposal_id: item.finding_id for item in promotions}
            sequence = int(session.scalar(select(func.coalesce(func.max(ArchitectureReviewRecord.sequence), 0)).where(
                ArchitectureReviewRecord.run_id == run_id
            )) or 0) + 1
            evidence_artifacts, index_path, index_markdown = self._build_reviewer_evidence_index(
                session, run, review_id
            )
            delta_markdown = "\n\n".join(
                "\n".join([
                    f"### {item.delta_id} · {item.change_kind}",
                    f"Node: {item.node_id or 'run'}",
                    f"Node Version: {item.node_version or '-'}",
                    f"Graph Version: {item.graph_version}",
                    f"Root Relevant: {'yes' if item.root_relevant else 'no'}",
                    f"Evidence: {', '.join(item.evidence_refs or []) or '无'}",
                    "Change: " + json.dumps(item.new_summary or {}, ensure_ascii=False),
                ])
                for item in deltas
            ) or "无。"
            communication_markdown = "\n\n".join(
                "\n".join([
                    f"### {item.communication_id} · {item.title}",
                    f"Attention: {item.attention}",
                    f"Source Node: {item.node_id} v{item.node_version}",
                    f"Source Graph Version: {item.graph_version}",
                    f"Question: {item.question}",
                    f"Global Consequence: {item.global_consequence}",
                    f"Affected Nodes: {', '.join(item.affected_node_ids or []) or '无'}",
                    f"Related Findings or Evidence: {', '.join(item.related_evidence_ids or []) or '无'}",
                    f"Local Pressure Tests: {item.local_pressure_tests}",
                    f"Rationale: {item.rationale}",
                ])
                for item in communications
            ) or "无。"
            ledger_markdown = "\n\n".join(
                f"### D-{item.sequence:03d} · {item.title}\n{item.markdown}"
                for item in ledger
            ) or "无。"
            promotions_markdown = "\n\n".join(
                f"### {item.proposal_id}\nFinding ID: {item.finding_id}\nRationale: {item.supervisor_rationale}\nRecommended: {json.dumps(item.recommended_actions, ensure_ascii=False)}"
                for item in promotions
            ) or "无。"
            nodes = repo.list_nodes(run_id)
            return {
                "user_id": run.owner_user_id,
                "attempt_id": run.attempts[-1].attempt_id,
                "contract": self.artifacts.read_text(run.contract_snapshot_path),
                "snapshot_markdown": self.artifacts.read_text(snapshot.snapshot_markdown_path),
                "snapshot_revision": snapshot.revision,
                "ledger_markdown": ledger_markdown,
                "delta_markdown": delta_markdown,
                "communication_markdown": communication_markdown,
                "promotions": promotions_markdown,
                "promotion_findings": promotion_findings,
                "verified_claims": self._claims_markdown(repo.list_verified_claims(run_id)) or "无。",
                "active_count": len([node for node in nodes if node.status in {"active", "draining"}]),
                "ready_count": len([node for node in nodes if node.status == "ready"]),
                "promotion_slots": max(0, int(self.settings.max_promotions_per_review)),
                "graph_version": run.graph_version,
                "node_preconditions": {
                    node.node_id: {
                        "node_version": node.node_version,
                        "status": node.status,
                    }
                    for node in nodes
                },
                "review_sequence": sequence,
                "delta_ids": [item.delta_id for item in deltas],
                "communication_ids": [item.communication_id for item in communications],
                "evidence_artifacts": evidence_artifacts,
                "artifact_index_path": index_path,
                "artifact_index_markdown": index_markdown,
            }

    def _build_reviewer_evidence_index(
        self,
        session: Any,
        run: ResearchRunRecord,
        review_id: str,
    ) -> tuple[list[dict[str, object]], Path, str]:
        """冻结当前 Run 的可读证据清单；模型只能按 ID 读取，不能提交任意路径。"""
        repo = RepositoryHub(session, self.artifacts)
        root = self._run_root(run.run_id) / "runtime" / "architecture" / "review-inputs" / review_id
        root.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, object]] = []
        seen: set[Path] = set()

        def add(path: Path, kind: str, title: str) -> None:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                return
            if resolved.suffix.lower() not in {".md", ".txt", ".tex", ".json"}:
                return
            seen.add(resolved)
            artifacts.append({
                "artifact_id": f"artifact-{len(artifacts) + 1:04d}",
                "kind": kind,
                "title": title,
                "path": str(resolved),
                "sha256": sha256_file(resolved),
            })

        for node in repo.list_nodes(run.run_id):
            details = dict(node.details or {})
            latest = str(details.get("latest_supervisor_review_path") or "")
            if latest:
                add(self.artifacts.resolve(latest), "supervisor_review", f"{node.node_id} latest Supervisor Review")
            if node.final_stage_path:
                add(self.artifacts.resolve(node.final_stage_path), "final_local_stage", f"{node.node_id} Final Local Stage")
            note = str(details.get("supervisor_notes") or "")
            if note:
                note_path = root / f"private-notes-{node.node_id}.md"
                self.artifacts.write_text(note_path.relative_to(self.artifacts.data_root), f"# {node.node_id} Supervisor Private Notes\n\n{note}\n")
                add(note_path, "private_notes", f"{node.node_id} Supervisor private notes")
            for candidate_id, payload in dict(details.get("candidate_artifacts") or {}).items():
                candidate_path = str(dict(payload).get("path") or "")
                if candidate_path:
                    add(self.artifacts.resolve(candidate_path), "claim_candidate", f"{node.node_id} {candidate_id}")
            reports = session.scalars(
                select(WorkerReportRecord).join(WorkerAssignmentRecord).where(
                    WorkerAssignmentRecord.node_id == node.node_id
                ).order_by(WorkerReportRecord.created_at)
            )
            for report in reports:
                add(self.artifacts.resolve(report.markdown_path), "worker_report", f"{node.node_id} Worker report {report.report_id}")
            for finding in session.scalars(select(ResearchFindingRecord).where(
                ResearchFindingRecord.node_id == node.node_id
            ).order_by(ResearchFindingRecord.created_at)):
                finding_path = root / f"finding-{finding.finding_id}.md"
                self.artifacts.write_text(
                    finding_path.relative_to(self.artifacts.data_root),
                    f"# {finding.title}\n\nType: {finding.finding_type}\nSignificance: {finding.significance}\nStatus: {finding.status}\n\n{finding.markdown}\n",
                )
                add(finding_path, "finding", f"{node.node_id} {finding.title}")
            literature_root = self._node_root(run.run_id, node.node_id) / "literature"
            if literature_root.is_dir():
                for path in sorted(literature_root.rglob("*")):
                    add(path, "literature_evidence", f"{node.node_id} literature {path.name}")
        for claim in repo.list_verified_claims(run.run_id):
            if claim.candidate_path:
                add(self.artifacts.resolve(claim.candidate_path), "verified_candidate", f"{claim.verified_claim_id} candidate")
            if claim.verifier_report_path:
                add(self.artifacts.resolve(claim.verifier_report_path), "verifier_report", f"{claim.verified_claim_id} verifier report")
        for review in session.scalars(select(ArchitectureReviewRecord).where(
            ArchitectureReviewRecord.run_id == run.run_id
        ).order_by(ArchitectureReviewRecord.sequence)):
            add(self.artifacts.resolve(review.decision_markdown_path), "prior_architecture_review", f"Prior Architecture Review {review.sequence}")
        source_root = self.artifacts.resolve(run.source_snapshot_path)
        if source_root.is_dir():
            for path in sorted(source_root.iterdir()):
                add(path, "source_artifact", path.name)

        lines = ["# Reviewer Artifact Index", "", "| Artifact ID | Kind | Title | SHA256 |", "|---|---|---|---|"]
        lines.extend(
            f"| {item['artifact_id']} | {item['kind']} | {str(item['title']).replace('|', '/')} | {item['sha256']} |"
            for item in artifacts
        )
        index_path = root / "artifact-index.md"
        index_markdown = "\n".join(lines) + "\n"
        self.artifacts.write_text(index_path.relative_to(self.artifacts.data_root), index_markdown)
        return artifacts, index_path, index_markdown

    @staticmethod
    def _apply_promotion_decisions(repo: RepositoryHub, decision: ArchitectureDecision) -> None:
        """把 Reviewer 对每个 proposal 的显式决定写回状态，避免永久重复呈现。"""
        for item in decision.promotion_decisions:
            proposal = repo.session.get(PromotionProposalRecord, item.proposal_id)
            if proposal is None:
                continue
            proposal.status = {
                "accept": "accepted",
                "merge": "accepted",
                "return_local": "returned_local",
                "defer": "deferred",
                "reject": "rejected",
            }[item.decision]
            if item.decision == "return_local" and proposal.finding is not None:
                proposal.finding.status = "local"

    @staticmethod
    def _append_ledger_updates(
        repo: RepositoryHub,
        run: ResearchRunRecord,
        review_id: str,
        markdown: str,
    ) -> None:
        """把 Reviewer 的稀疏 Ledger 更新按三级标题追加为独立条目。"""
        if markdown.strip().lower() in {"", "无", "无。", "暂无", "暂无。", "none"}:
            return
        matches = list(re.finditer(r"^###\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
        if not matches:
            repo.append_architecture_ledger(
                run, source_review_id=review_id, title="Fresh Reviewer decision", markdown=markdown
            )
            return
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            repo.append_architecture_ledger(
                run,
                source_review_id=review_id,
                title=match.group(1).strip(),
                markdown=markdown[start:end].strip(),
            )

    def _final_evidence_snapshot(self, run_id: str, completion_kind: str) -> str:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise DynamicProverError("Run 不存在")
            nodes = repo.list_nodes(run_id)
            findings = list(session.scalars(select(ResearchFindingRecord).join(ResearchNodeRecord).where(ResearchNodeRecord.run_id == run_id).order_by(ResearchFindingRecord.created_at)))
            source_manifest = self._read_public_artifact(self.artifacts.resolve(run.source_snapshot_path).parent / "source_manifest.json")
            stage_blocks: list[str] = []
            for node in nodes:
                stage_text = self._read_public_artifact(node.final_stage_path) if node.final_stage_path else ""
                latest_diagnosis = str((node.details or {}).get("latest_supervisor_diagnosis") or "")
                stage_blocks.append(
                    "\n".join([
                        f"## {node.node_id} · {node.title}",
                        f"Kind: {node.kind}",
                        f"Status: {node.status}",
                        f"Objective: {node.objective}",
                        f"Completion Criterion: {node.completion_criterion}",
                        "",
                        "### Final Local Stage",
                        stage_text or latest_diagnosis or "无可用 Final Local Stage；请仅依赖其他公开 Evidence。",
                    ])
                )
            claim_blocks: list[str] = []
            for claim in repo.list_verified_claims(run_id):
                claim_blocks.append(
                    "\n".join([
                        f"## {claim.verified_claim_id} · Node {claim.node_id}",
                        f"Verdict: {claim.verdict}",
                        "",
                        "### Exact Statement",
                        claim.statement,
                        "",
                        "### Verified Candidate Artifact",
                        self._read_public_artifact(claim.candidate_path) or "[candidate artifact unavailable]",
                        "",
                        "### Fresh Verifier Report",
                        self._read_public_artifact(claim.verifier_report_path) or "[verifier report unavailable]",
                    ])
                )
            return "\n\n".join([
                f"# Completion Kind\n\n{completion_kind}",
                "# Research Contract\n\n" + self.artifacts.read_text(run.contract_snapshot_path),
                "# Frozen Source Manifest\n\n" + (source_manifest or "无。"),
                "# Verified Claims and Their Public Proof Evidence\n\n" + ("\n\n".join(claim_blocks) or "无。"),
                "# Node Status\n\n" + "\n".join(f"- {node.node_id} [{node.kind}] {node.title}: {node.status}" for node in nodes),
                "# Final Local Stages\n\n" + ("\n\n".join(stage_blocks) or "无。"),
                "# Public Findings\n\n" + "\n\n".join(f"## {item.title}\nType: {item.finding_type}\nStatus: {item.status}\n\n{item.markdown}" for item in findings),
            ])

    def _read_public_artifact(self, reference: str | Path, *, max_chars: int = 200_000) -> str:
        """Read a public text artifact for the final Evidence Manifest.

        The Writer must receive mathematical content rather than a list of opaque file
        paths.  A generous per-artifact cap prevents an accidental raw stream/log from
        dominating the final prompt; truncation is explicit and therefore auditable.
        """
        path = self.artifacts.resolve(reference)
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return f"[unreadable public artifact: {path.name}]"
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n[artifact truncated after {max_chars} characters]"

    # ------------------------------------------------------------------ state helpers
    def _ready_assignments(self, node_id: str) -> list[Any]:
        with self.database.session() as session:
            items = list(session.scalars(select(WorkerAssignmentRecord).where(
                WorkerAssignmentRecord.node_id == node_id,
                WorkerAssignmentRecord.status == "ready",
            ).order_by(WorkerAssignmentRecord.created_at)))
            return [self._detach_assignment(item) for item in items]

    def _get_node(self, node_id: str) -> Any:
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                raise DynamicProverError("Node 不存在")
            return self._detach_node(node)

    def _get_worker(self, worker_id: str) -> Any:
        with self.database.session() as session:
            worker = session.get(WorkerIdentityRecord, worker_id)
            if worker is None:
                raise DynamicProverError("Worker 不存在")
            return self._detach_worker(worker)

    def _set_node_status(self, node_id: str, status: str, *, error: str | None = None) -> None:
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node:
                node.status = status
                node.node_version += 1
                if status in TERMINAL_NODE_STATES:
                    now = utc_now()
                    for assignment in session.scalars(select(WorkerAssignmentRecord).where(
                        WorkerAssignmentRecord.node_id == node_id,
                        WorkerAssignmentRecord.status.in_(["ready", "running"]),
                    )):
                        assignment.status = "cancelled" if assignment.status == "ready" else "interrupted"
                        assignment.completed_at = now
                    for worker in session.scalars(select(WorkerIdentityRecord).where(
                        WorkerIdentityRecord.node_id == node_id,
                        WorkerIdentityRecord.status.in_(["assigned", "running", "draining"]),
                    )):
                        if status == "retired" or worker.status == "draining":
                            worker.status = "retired"
                            worker.retired_at = now
                        else:
                            worker.status = "idle"
                if error:
                    details = dict(node.details or {})
                    details["last_error"] = error
                    node.details = details

    def _mark_assignment_running(self, assignment_id: str, worker_id: str) -> None:
        with self.database.session() as session:
            assignment = session.get(WorkerAssignmentRecord, assignment_id)
            worker = session.get(WorkerIdentityRecord, worker_id)
            if assignment:
                assignment.status = "running"
                assignment.started_at = utc_now()
            if worker:
                worker.status = "running"

    def _mark_assignment_interrupted(self, assignment_id: str, worker_id: str) -> None:
        with self.database.session() as session:
            assignment = session.get(WorkerAssignmentRecord, assignment_id)
            worker = session.get(WorkerIdentityRecord, worker_id)
            if assignment and assignment.status == "running":
                assignment.status = "interrupted"
                assignment.completed_at = utc_now()
            if worker and worker.status != "retired":
                worker.status = "idle"

    def _mark_worker_task_failed(self, worker_id: str, error: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            worker = session.get(WorkerIdentityRecord, worker_id) if worker_id else None
            if worker:
                assignment = session.scalar(
                    select(WorkerAssignmentRecord)
                    .where(
                        WorkerAssignmentRecord.worker_id == worker_id,
                        WorkerAssignmentRecord.status.in_(["ready", "running"]),
                    )
                    .order_by(WorkerAssignmentRecord.assignment_version.desc())
                    .limit(1)
                )
                if assignment:
                    assignment.status = "failed"
                    assignment.completed_at = utc_now()
                operation = session.scalar(
                    select(AgentOperationCheckpointRecord)
                    .where(
                        AgentOperationCheckpointRecord.scope_type == "worker",
                        AgentOperationCheckpointRecord.scope_id == worker_id,
                        AgentOperationCheckpointRecord.status.in_([
                            "running", "capacity_wait", "paused",
                        ]),
                    )
                    .order_by(AgentOperationCheckpointRecord.updated_at.desc())
                    .limit(1)
                )
                if operation is not None:
                    repo.set_agent_operation_status(
                        operation.operation_id, "failed", error=error
                    )
                worker.status = "idle"
                node = session.get(ResearchNodeRecord, worker.node_id)
                if node:
                    details = dict(node.details or {})
                    details.setdefault("worker_errors", []).append({"worker_id": worker_id, "error": error})
                    node.details = details

    def _mark_run_started(self, user_id: str, run_id: str, attempt_id: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt is None or attempt.run_id != run_id:
                raise DynamicProverError("Run Attempt 不存在")
            if run.status != "draining":
                run.status = "running"
            run.started_at = run.started_at or utc_now()
            run.last_heartbeat_at = utc_now()
            attempt.status = "running"
            attempt.started_at = attempt.started_at or utc_now()
            repo.append_event("run", run_id, "run_started", f"Research Run Attempt {attempt.attempt_number} 开始")

    def _mark_run_failed(self, user_id: str, run_id: str, attempt_id: str, error: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            run.status = "failed"
            run.completed_at = utc_now(); run.terminal_at = run.completed_at
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt:
                attempt.status = "failed"
                attempt.error = error
                attempt.completed_at = utc_now()
            actual = repo.settle_run_cost(run_id)
            repo.append_event("run", run_id, "run_failed", error)
            repo.append_event("run", run_id, "run_cost_settled", "Technical failure usage was charged", {"actual_cost": actual})
        self.final_snapshots.export_run(run_id)

    def _mark_attempt_interrupted(self, user_id: str, run_id: str, attempt_id: str, reason: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.get_run(user_id, run_id)
            if run.status != "completed":
                run.status = "interrupted"
                run.terminal_at = utc_now()
            attempt = session.get(RunAttemptRecord, attempt_id)
            if attempt and attempt.status != "completed":
                attempt.status = "interrupted"
                attempt.error = reason
                attempt.completed_at = utc_now()
            repo.append_event("run", run_id, "run_interrupted", reason)

    def _mark_shutdown_runs_interrupted(self, run_ids: set[str]) -> None:
        if not run_ids:
            return
        with self.database.session() as session:
            runs = list(session.scalars(select(ResearchRunRecord).where(ResearchRunRecord.run_id.in_(run_ids), ResearchRunRecord.status.in_(["queued", "resuming", "running", "draining"]))))
            for run in runs:
                run.status = "interrupted"
                run.terminal_at = utc_now()
                if run.attempts and run.attempts[-1].status == "running":
                    run.attempts[-1].status = "interrupted"
                    run.attempts[-1].error = "MathLab service stopped"
                    run.attempts[-1].completed_at = utc_now()

    def _reconcile_abandoned_runs(self) -> None:
        with self.database.session() as session:
            paused_runs = list(session.scalars(select(ResearchRunRecord).where(
                ResearchRunRecord.status.in_(["paused", "resuming"])
            )))
            for run in paused_runs:
                run.status = "paused"
                run.claimed_by_runner = None
                run.claimed_at = None
                run.resume_requested_at = None
                if run.paused_at is None:
                    run.paused_at = utc_now()
                if run.attempts:
                    run.attempts[-1].status = "paused"
                for operation in run.agent_operations:
                    if operation.status in {"running", "capacity_wait"}:
                        operation.status = "paused"
                        operation.pause_epoch = run.pause_epoch
                        operation.updated_at = utc_now()
            runs = list(session.scalars(select(ResearchRunRecord).where(ResearchRunRecord.status.in_(["running", "draining"]))))
            for run in runs:
                if run.pause_requested_at is not None:
                    run.pre_pause_status = run.status
                    run.pre_pause_phase = run.pre_pause_phase or "research"
                    run.status = "paused"
                    run.paused_at = run.paused_at or utc_now()
                    run.claimed_by_runner = None
                    run.claimed_at = None
                    if run.attempts:
                        run.attempts[-1].status = "paused"
                    for operation in run.agent_operations:
                        if operation.status in {"running", "capacity_wait"}:
                            operation.status = "paused"
                    continue
                if run.graceful_drain_requested_at is not None:
                    run.status = "queued"
                    run.claimed_by_runner = None
                    run.claimed_at = None
                    if run.attempts:
                        attempt = run.attempts[-1]
                        attempt.status = "created"
                        attempt.error = None
                        attempt.completed_at = None
                    continue
                run.status = "interrupted"
                run.terminal_at = utc_now()
                if run.attempts and run.attempts[-1].status == "running":
                    attempt = run.attempts[-1]
                    attempt.status = "interrupted"
                    attempt.error = "Previous MathLab process ended; technical retry is allowed"
                    attempt.completed_at = utc_now()

    # ------------------------------------------------------------------ serialization
    @staticmethod
    def _controlling_proposition_id(node_id: str) -> str:
        """为 Claim Node 生成稳定控制命题身份；该身份不随 Local Review 变化。"""
        return f"claim-proposition:{node_id}"

    def _pending_worker_report_inbox(
        self, node_id: str
    ) -> tuple[list[tuple[str, WorkerReport]], list[str]]:
        """从数据库重建 Supervisor 未消费报告，内存列表不再是唯一真相。"""
        reports: list[tuple[str, WorkerReport]] = []
        report_ids: list[str] = []
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            for item in repo.pending_worker_reports(node_id):
                path = self.artifacts.resolve(item.markdown_path)
                if not path.is_file() or sha256_file(path) != item.sha256:
                    raise DynamicProverError(
                        f"pending Worker Report 缺失或 SHA 漂移：{item.report_id}"
                    )
                reports.append((
                    item.assignment_id,
                    WorkerReport(
                        status=item.status,
                        major_event=bool(item.major_event),
                        major_event_type=None,
                        raw_markdown=path.read_text(encoding="utf-8"),
                    ),
                ))
                report_ids.append(item.report_id)
        return reports, report_ids

    def _pending_verification_feedback(
        self, node_id: str
    ) -> tuple[str, list[str]]:
        """读取尚未由 Node Supervisor 消费的 fresh-verifier 反馈。"""
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return "", []
            pending = [
                dict(item)
                for item in list((node.details or {}).get("verification_feedback_inbox") or [])
                if str(dict(item).get("status") or "pending") == "pending"
            ]
        text = "\n\n".join(
            f"### {item.get('feedback_id')} · Candidate {item.get('candidate_artifact_id')}\n\n"
            f"{item.get('markdown') or item.get('error') or '无可用反馈。'}"
            for item in pending
        )
        return text, [str(item.get("feedback_id")) for item in pending]

    def _record_verification_feedback(
        self,
        run_id: str,
        node_id: str,
        candidate_id: str,
        *,
        verdict: str,
        markdown: str,
        error: str = "",
    ) -> str:
        """将 PASS/REJECT/UNAVAILABLE 反馈写入 Node durable inbox 供恢复后继续。"""
        feedback_id = new_id("verification_feedback")
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            node = session.get(ResearchNodeRecord, node_id)
            run = session.get(ResearchRunRecord, run_id)
            if node is None or run is None:
                return feedback_id
            details = dict(node.details or {})
            inbox = list(details.get("verification_feedback_inbox") or [])
            if any(
                str(dict(item).get("candidate_artifact_id") or "") == candidate_id
                and str(dict(item).get("verdict") or "") == verdict
                and str(dict(item).get("status") or "pending") == "pending"
                for item in inbox
            ):
                return str(next(
                    dict(item).get("feedback_id") for item in inbox
                    if str(dict(item).get("candidate_artifact_id") or "") == candidate_id
                    and str(dict(item).get("verdict") or "") == verdict
                    and str(dict(item).get("status") or "pending") == "pending"
                ))
            inbox.append({
                "feedback_id": feedback_id,
                "candidate_artifact_id": candidate_id,
                "verdict": verdict,
                "markdown": markdown,
                "error": error,
                "status": "pending",
                "created_at": utc_now().isoformat(),
            })
            details["verification_feedback_inbox"] = inbox[-40:]
            node.details = details
            repo.add_global_state_delta(
                run,
                node=node,
                change_kind="claim_verification_feedback",
                new_summary={
                    "candidate_artifact_id": candidate_id,
                    "verdict": verdict,
                    "feedback_id": feedback_id,
                },
                evidence_refs=[candidate_id],
                root_relevant=False,
            )
        return feedback_id

    def _durable_local_review_count(self, node_id: str) -> int:
        """以数据库计数和历史 review artifact 的较大值恢复 Local Review 熔断器。"""
        # review-001 is the Supervisor bootstrap and does not consume the configured
        # Local Review repair budget.
        artifact_count = max(0, self._count_supervisor_artifacts(node_id) - 1)
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return artifact_count
            durable = max(int(node.local_review_count or 0), artifact_count)
            if node.local_review_count != durable:
                node.local_review_count = durable
            return durable

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        cleaned = text.strip()
        match = re.fullmatch(r"```(?:markdown)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
        first = cleaned.find("# ")
        if first > 0:
            cleaned = cleaned[first:]
        return cleaned.rstrip() + "\n"

    @staticmethod
    def _report_context(reports: list[tuple[str, WorkerReport]]) -> str:
        return "\n\n".join(f"### Assignment {assignment_id}\n\n{report.raw_markdown}" for assignment_id, report in reports)

    @staticmethod
    def _claims_markdown(claims: list[Any]) -> str:
        return "\n\n".join(
            f"### {item.verified_claim_id}\n"
            f"Verdict: {item.verdict}\n"
            f"Node: {item.node_id}\n"
            f"数学范围: {'当前节点控制命题' if item.candidate_scope == 'controlling_node_claim' else '辅助事实'}\n\n"
            f"{item.statement}\n\n"
            f"Reuse Boundary: {item.reuse_boundary or '仅限该精确陈述及其已声明假设。'}"
            for item in claims
        )

    @staticmethod
    def _contract_future_sections(contract: str) -> str:
        start = contract.find("## E. 第二层")
        if start < 0:
            return "请参考完整 Contract 的 Structured/Directional 部分。"
        return contract[start:]

    def _run_root(self, run_id: str) -> Path:
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                if self.settings.test_auth_bypass:
                    path = self.artifacts.platform_root / "test-unbound-runs" / run_id
                    path.mkdir(parents=True, exist_ok=True)
                    return path
                raise DynamicProverError("Research Run 不存在")
            path = self.artifacts.resolve(run.run_root)
        if not path.is_relative_to(self.artifacts.users_root):
            raise DynamicProverError("Research Run 不在 user-scoped 存储根")
        return path

    def _run_runtime_root(self, run_id: str) -> Path:
        return self._run_root(run_id) / "runtime"

    def _node_root(self, run_id: str, node_id: str) -> Path:
        path = self._run_root(run_id) / "runtime" / "nodes" / node_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _next_local_review_number(self, node_id: str) -> int:
        return self._count_supervisor_artifacts(node_id) + 1

    def _count_supervisor_artifacts(self, node_id: str) -> int:
        # Artifact count is stable and independent of prompt output parsing.
        with self.database.session() as session:
            node = session.get(ResearchNodeRecord, node_id)
            if node is None:
                return 0
            run_id = node.run_id
        supervisor_root = self._run_root(run_id) / "runtime" / "nodes" / node_id / "supervisor"
        return len(list(supervisor_root.glob("review-*.md"))) if supervisor_root.is_dir() else 0

    @staticmethod
    def _detach_node(node: ResearchNodeRecord) -> Any:
        class View: pass
        view = View()
        for name in ("node_id", "run_id", "kind", "title", "objective", "completion_criterion", "status", "details", "node_version", "final_stage_path"):
            setattr(view, name, getattr(node, name))
        return view

    @staticmethod
    def _detach_worker(worker: WorkerIdentityRecord) -> Any:
        class View: pass
        view = View()
        for name in ("worker_id", "node_id", "persona", "worker_label", "status", "provider_thread_id", "assignment_counter"):
            setattr(view, name, getattr(worker, name))
        return view

    @staticmethod
    def _detach_assignment(item: WorkerAssignmentRecord) -> Any:
        class View: pass
        view = View()
        for name in ("assignment_id", "node_id", "worker_id", "persona", "worker_label", "assignment_version", "task_markdown", "status", "report_count"):
            setattr(view, name, getattr(item, name))
        return view

    @staticmethod
    def _detach_operation(item: AgentOperationCheckpointRecord) -> Any:
        class View: pass
        view = View()
        for name in (
            "operation_id", "operation_kind", "role", "role_key", "scope_type",
            "scope_id", "provider_thread_id", "task_path", "payload", "repair_number",
        ):
            setattr(view, name, getattr(item, name))
        return view

    @staticmethod
    def _node_payload(node: ResearchNodeRecord) -> dict[str, Any]:
        public_card = dict((node.details or {}).get("public_card") or {})
        display_summary = (
            node.objective
            if public_card.get("fallback")
            else public_card.get("summary") or node.objective
        )
        return {
            "node_id": node.node_id, "parent_node_id": node.parent_node_id,
            "kind": node.kind, "title": node.title, "objective": node.objective,
            "display_title": public_card.get("title") or node.title,
            "display_summary": display_summary,
            "display_status": public_card.get("status") or "legacy",
            "completion_criterion": node.completion_criterion, "status": node.status,
            "node_version": node.node_version, "details": node.details,
            "final_stage_path": node.final_stage_path,
        }

    @staticmethod
    def _worker_payload(worker: WorkerIdentityRecord) -> dict[str, Any]:
        return {
            "worker_id": worker.worker_id, "node_id": worker.node_id,
            "persona": worker.persona, "worker_label": worker.worker_label,
            "status": worker.status, "assignment_counter": worker.assignment_counter,
            "persistent_thread": bool(worker.provider_thread_id),
        }

    @staticmethod
    def _assignment_payload(item: WorkerAssignmentRecord) -> dict[str, Any]:
        return {
            "assignment_id": item.assignment_id, "node_id": item.node_id,
            "worker_id": item.worker_id, "persona": item.persona,
            "worker_label": item.worker_label, "assignment_version": item.assignment_version,
            "status": item.status, "task": item.task_markdown,
        }
