from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import asyncio
import json
import re
import shutil

from pypdf import PdfReader
from math_planner.codex_exec import CodexExecError
from math_planner.contract_app_server import ContractAppServerError

from .agent_backend import AgentTurn, ContractCodexBackend
from .artifacts import ArtifactStore, sha256_file
from .contract_schema import (
    ContractDocument,
    ContractValidation,
    contract_fragments,
    extract_initial_nodes,
    parse_contract,
    replace_contract_fragment,
    render_contract,
    validate_contract,
    validate_fragment_structure,
)
from .db import (
    ContractConversationRecord,
    ContractGenerationAttemptRecord,
    ContractVersionRecord,
    Database,
    new_id,
    utc_now,
)
from .repositories import RepositoryHub
from .languages import normalize_language
from .settings import VNextSettings


class ContractWorkflowError(RuntimeError):
    pass


class ContractGenerationInterrupted(ContractWorkflowError):
    pass


@dataclass(frozen=True)
class GeneratedContract:
    conversation_id: str
    version: int
    contract_version_id: str
    markdown: str
    warnings: list[str]
    execution_policy: dict[str, Any]


@dataclass(frozen=True)
class QueuedGeneration:
    conversation_id: str
    generation_attempt_id: str
    status: str
    attempt_number: int


ChatStreamEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


class ReplyStreamProjector:
    """只把结构化模型输出的 Reply 区段投影到用户可见流。"""

    _reply_heading = re.compile(r"(?:^|\n)## Reply[ \t]*\r?\n")
    _patch_heading = re.compile(r"\n## Draft Patches(?:[ \t]*\r?\n|[ \t]*$)")

    def __init__(self, emit: ChatStreamEvent | None) -> None:
        self.emit = emit
        self.raw = ""
        self.visible = ""

    async def feed(self, delta: str) -> None:
        """累积 transport delta，并保留尾部以免内部区段标题短暂泄漏。"""
        if not delta or self.emit is None:
            self.raw += delta
            return
        self.raw += delta
        match = self._reply_heading.search(self.raw)
        if match is None:
            return
        start = match.end()
        suffix = self.raw[start:]
        patch = self._patch_heading.search(suffix)
        if patch is not None:
            projected = suffix[: patch.start()]
        else:
            reserve = len("\n## Draft Patches") - 1
            projected = suffix[: max(0, len(suffix) - reserve)]
        projected = projected.lstrip("\r\n")
        await self._publish(projected)

    async def finish(self, validated_reply: str) -> None:
        """以最终通过 parser 的 Reply 补齐被尾部保护暂存的字符。"""
        if self.emit is None:
            return
        await self._publish(validated_reply)

    async def _publish(self, projected: str) -> None:
        if projected == self.visible:
            return
        if projected.startswith(self.visible):
            delta = projected[len(self.visible) :]
            self.visible = projected
            if delta:
                await self.emit("delta", {"delta": delta})
            return
        self.visible = projected
        await self.emit("reset", {"content": projected})


class ContractService:
    """Contract-first Planner service.

    Web/API methods create immutable input snapshots and queue generation attempts.
    The Runner executes the expensive Contractor/Auditor/Integrator pipeline.  A
    small compatibility wrapper remains for deterministic tests and local probes.
    """

    def __init__(
        self,
        settings: VNextSettings,
        database: Database,
        artifacts: ArtifactStore,
        backend: ContractCodexBackend,
        execution_model_profiles: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.artifacts = artifacts
        self.backend = backend
        self.execution_model_profiles = execution_model_profiles or {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _resolve_execution_models(self, policy: dict[str, Any]) -> dict[str, Any]:
        """在批准时把 profile ID 解析成 Run 内不可变的逐角色模型快照。"""
        resolved = dict(policy)
        profile_name = str(resolved.get("live_mapping") or "")
        profile = self.execution_model_profiles.get(profile_name)
        if profile is None:
            if self.settings.offline_agents:
                return resolved
            raise ContractWorkflowError(f"Execution model profile `{profile_name}` 不存在")
        resolved["resolved_role_models"] = {
            role: {
                "model": str(model.get("model") or ""),
                "reasoning_effort": str(model.get("reasoning_effort") or ""),
            }
            for role, model in profile.items()
        }
        return resolved

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    # ------------------------------------------------------------------
    # Research Request draft / canonical snapshot
    # ------------------------------------------------------------------
    @staticmethod
    def canonical_research_request(
        *,
        research_problem: str,
        background_and_prior_progress: str = "",
        method_preferences_and_restrictions: str = "",
        valuable_partial_outcomes: str = "",
        possible_ideas_and_rough_approaches: str = "",
    ) -> str:
        if not research_problem.strip():
            raise ContractWorkflowError("“想研究的问题”不能为空")
        return (
            "# Research Request\n\n"
            "## Research Problem\n\n"
            f"{research_problem.strip()}\n\n"
            "## Background and Prior Progress\n\n"
            f"{background_and_prior_progress.strip() or '未提供。'}\n\n"
            "## Method Preferences and Restrictions\n\n"
            f"{method_preferences_and_restrictions.strip() or '未提供。'}\n\n"
            "## Valuable Partial Outcomes\n\n"
            f"{valuable_partial_outcomes.strip() or '未提供。'}\n\n"
            "## Possible Ideas and Rough Approaches\n\n"
            f"{possible_ideas_and_rough_approaches.strip() or '未提供；不得据此推断用户认可任何未经验证的事实。'}\n"
        )

    def update_request_draft(
        self,
        user_id: str,
        conversation_id: str,
        *,
        expected_revision: int,
        research_problem: str,
        background_and_prior_progress: str,
        method_preferences_and_restrictions: str,
        valuable_partial_outcomes: str,
        possible_ideas_and_rough_approaches: str,
        contractor_profile: str,
        project_id: str | None,
        source_file_ids: list[str],
        display_name: str | None = None,
        contract_language: str | None = None,
    ) -> dict[str, Any]:
        canonical = self.canonical_research_request(
            research_problem=research_problem,
            background_and_prior_progress=background_and_prior_progress,
            method_preferences_and_restrictions=method_preferences_and_restrictions,
            valuable_partial_outcomes=valuable_partial_outcomes,
            possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches,
        )
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            item = repo.update_request_draft(
                user_id,
                conversation_id,
                expected_revision=expected_revision,
                research_request=canonical,
                research_problem=research_problem,
                background_and_prior_progress=background_and_prior_progress,
                method_preferences_and_restrictions=method_preferences_and_restrictions,
                valuable_partial_outcomes=valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches,
                contractor_profile=contractor_profile,
                contract_language=contract_language,
                project_id=project_id,
                source_file_ids=source_file_ids,
                display_name=display_name,
            )
            if int(item.request_draft_revision) != int(expected_revision):
                repo.append_event(
                    "conversation",
                    conversation_id,
                    "research_request_draft_saved",
                    "Research Request Draft 已暂存",
                    {"draft_revision": item.request_draft_revision},
                )
            return self._request_draft_payload(item)

    def save_request_draft(
        self,
        user_id: str,
        *,
        conversation_id: str | None,
        expected_revision: int,
        research_problem: str,
        background_and_prior_progress: str,
        method_preferences_and_restrictions: str,
        valuable_partial_outcomes: str,
        possible_ideas_and_rough_approaches: str,
        contractor_profile: str,
        project_id: str | None,
        source_file_ids: list[str],
        display_name: str | None = None,
        contract_language: str | None = None,
    ) -> dict[str, Any]:
        canonical = self.canonical_research_request(
            research_problem=research_problem,
            background_and_prior_progress=background_and_prior_progress,
            method_preferences_and_restrictions=method_preferences_and_restrictions,
            valuable_partial_outcomes=valuable_partial_outcomes,
            possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches,
        )
        if conversation_id is None:
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                item = repo.create_conversation(
                    user_id,
                    canonical,
                    project_id=project_id,
                    display_name=display_name,
                    source_file_ids=source_file_ids,
                    research_problem=research_problem,
                    background_and_prior_progress=background_and_prior_progress,
                    method_preferences_and_restrictions=method_preferences_and_restrictions,
                    valuable_partial_outcomes=valuable_partial_outcomes,
                    possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches,
                    contractor_profile=contractor_profile,
                    contract_language=contract_language,
                    status="request_draft",
                )
                return self._request_draft_payload(item)
        return self.update_request_draft(
            user_id,
            conversation_id,
            expected_revision=expected_revision,
            research_problem=research_problem,
            background_and_prior_progress=background_and_prior_progress,
            method_preferences_and_restrictions=method_preferences_and_restrictions,
            valuable_partial_outcomes=valuable_partial_outcomes,
            possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches,
            contractor_profile=contractor_profile,
            contract_language=contract_language,
            project_id=project_id,
            source_file_ids=source_file_ids,
            display_name=display_name,
        )

    def execution_policy_catalog(self) -> dict[str, object]:
        return self.settings.execution_catalog()

    def generation_status(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            item = repo.latest_generation_attempt(user_id, conversation_id)
            if item is None:
                return {"conversation_id": conversation_id, "attempt": None}
            conversation = repo.get_conversation(user_id, conversation_id)
            pending = item.status in {"queued", "running", "repairing", "interrupted"}
            stages = repo.ensure_generation_stages(item.generation_attempt_id)
            return {
                "conversation_id": conversation_id,
                "attempt": {
                    "generation_attempt_id": item.generation_attempt_id,
                    "attempt_number": item.attempt_number,
                    "kind": item.kind,
                    "status": item.status,
                    "contractor_profile": item.contractor_profile,
                    "resulting_contract_version_id": item.resulting_contract_version_id,
                    "target_version": (
                        int(conversation.latest_contract_version) + 1 if pending else None
                    ),
                    "resumable": item.status == "interrupted",
                    "phases": self._generation_phase_payload(stages),
                    "error": item.error,
                    "expected_credit": float(item.expected_credit or 0),
                    "precharged_credit": float(item.precharged_credit or 0),
                    "actual_credit": float(item.actual_credit or 0),
                    "settled_credit": float(item.settled_credit or 0),
                    "elapsed_seconds": self.generation_elapsed_seconds(item),
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                },
            }

    @staticmethod
    def generation_elapsed_seconds(item: ContractGenerationAttemptRecord) -> int:
        """计算用户从提交 Contract generation 起已经等待的墙钟秒数。

        SQLite 读取出的 timezone-aware 字段可能丢失 tzinfo，因此统一按 UTC
        解释；完成或失败的 Attempt 使用完成时间封顶，其余状态持续累计。
        """
        if item.created_at is None:
            return 0

        def as_utc(value: datetime) -> datetime:
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

        terminal = item.status in {"completed", "failed"} and item.completed_at is not None
        end = as_utc(item.completed_at) if terminal else utc_now()
        return max(0, int((end - as_utc(item.created_at)).total_seconds()))

    @staticmethod
    def _generation_phase_payload(stages: list[Any]) -> list[dict[str, Any]]:
        """把六个 durable checkpoint 聚合为用户可见的三阶段进度。"""
        by_key = {item.stage_key: item for item in stages}

        def aggregate(keys: tuple[str, ...]) -> tuple[str, int]:
            items = [by_key[key] for key in keys]
            completed = sum(item.status == "completed" for item in items)
            if completed == len(items):
                return "completed", completed
            if any(item.status == "running" for item in items):
                return "running", completed
            if any(item.status == "interrupted" for item in items):
                return "interrupted", completed
            return "waiting", completed

        specs = (
            ("contractor", ("contractor_stage1", "contractor_stage2")),
            (
                "auditors",
                ("auditor_fidelity", "auditor_completeness", "auditor_map"),
            ),
            ("integrator", ("integrator",)),
        )
        return [
            {
                "key": key,
                "status": aggregate(stage_keys)[0],
                "completed": aggregate(stage_keys)[1],
                "total": len(stage_keys),
            }
            for key, stage_keys in specs
        ]

    def resume_generation(
        self, user_id: str, conversation_id: str, generation_attempt_id: str
    ) -> QueuedGeneration:
        with self.database.session() as session:
            item = RepositoryHub(session, self.artifacts).resume_generation_attempt(
                user_id, conversation_id, generation_attempt_id
            )
            return QueuedGeneration(
                conversation_id,
                item.generation_attempt_id,
                item.status,
                item.attempt_number,
            )

    # ------------------------------------------------------------------
    # DB-backed Contract generation queue
    # ------------------------------------------------------------------
    def queue_initial_generation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> QueuedGeneration:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            if idempotency_key:
                existing = repo.find_generation_attempt_by_idempotency(user_id, idempotency_key)
                if existing is not None:
                    if existing.conversation_id != conversation_id or existing.kind != "initial":
                        raise ContractWorkflowError("幂等键已用于另一个 Contract generation 请求")
                    return QueuedGeneration(
                        conversation_id,
                        existing.generation_attempt_id,
                        existing.status,
                        existing.attempt_number,
                    )
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            if conversation.latest_contract_version:
                raise ContractWorkflowError("该对话已经存在 Contract；请使用完整重新生成或 Working Draft")
            canonical = self.canonical_research_request(
                research_problem=conversation.research_problem or conversation.research_request,
                background_and_prior_progress=conversation.background_and_prior_progress,
                method_preferences_and_restrictions=conversation.method_preferences_and_restrictions,
                valuable_partial_outcomes=conversation.valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=conversation.possible_ideas_and_rough_approaches,
            )
            conversation.research_request = canonical
            manifest = repo.source_manifest(user_id, conversation.source_file_ids)
            snapshot = {
                "kind": "initial",
                "conversation_id": conversation_id,
                "owner_user_id": user_id,
                "research_request": canonical,
                "source_manifest": manifest,
                "contractor_profile": conversation.contractor_profile,
                "contract_language": normalize_language(conversation.contract_language),
            }
            reserve = self.settings.contract_reserve(conversation.contractor_profile)
            self.settings.require_models_priced(self._contract_profile_models(conversation.contractor_profile))
            repo.assert_can_start_paid_operation(user_id, reserve)
            snapshot_path = self._generation_snapshot_path(user_id, conversation_id, "initial")
            self.artifacts.write_json(snapshot_path.relative_to(self.artifacts.data_root), snapshot)
            attempt = repo.create_generation_attempt(
                user_id,
                conversation_id,
                kind="initial",
                contractor_profile=conversation.contractor_profile,
                request_snapshot_path=snapshot_path,
                source_manifest=manifest,
                idempotency_key=idempotency_key,
                expected_credit=reserve,
            )
            return QueuedGeneration(
                conversation_id,
                attempt.generation_attempt_id,
                attempt.status,
                attempt.attempt_number,
            )

    def queue_full_regeneration(
        self,
        user_id: str,
        conversation_id: str,
        *,
        selected_version: int | None = None,
        user_instruction: str = "",
        idempotency_key: str | None = None,
    ) -> QueuedGeneration:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            if idempotency_key:
                existing = repo.find_generation_attempt_by_idempotency(user_id, idempotency_key)
                if existing is not None:
                    if existing.conversation_id != conversation_id or existing.kind != "full_regeneration":
                        raise ContractWorkflowError("幂等键已用于另一个 Contract generation 请求")
                    return QueuedGeneration(
                        conversation_id,
                        existing.generation_attempt_id,
                        existing.status,
                        existing.attempt_number,
                    )
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            base = repo.get_contract_version(user_id, conversation_id, selected_version)
            draft = conversation.working_draft
            # A Working Draft belongs to one immutable base version.  When the
            # user explicitly regenerates from a historical version, do not
            # silently mix in a Draft based on another version.
            include_draft = draft is not None and int(draft.base_version) == int(base.version)
            visible_chat = [
                {
                    "chat_message_id": item.chat_message_id,
                    "role": item.role,
                    "content": item.content,
                }
                for item in repo.list_contract_chat_messages(
                    user_id, conversation_id, base.contract_version_id
                )
            ]
            manifest = repo.source_manifest(user_id, conversation.source_file_ids)
            snapshot = {
                "kind": "full_regeneration",
                "conversation_id": conversation_id,
                "owner_user_id": user_id,
                "research_request": conversation.research_request,
                "base_contract_markdown": self.artifacts.read_text(base.markdown_path),
                "base_contract_version_id": base.contract_version_id,
                "base_contract_version": base.version,
                "working_draft": self._draft_payload(draft) if include_draft else None,
                "contract_chat": visible_chat,
                "user_revision_instruction": user_instruction.strip(),
                "source_manifest": manifest,
                "contractor_profile": conversation.contractor_profile,
                "contract_language": normalize_language(conversation.contract_language),
                "execution_policy": dict(conversation.execution_policy or {}),
            }
            reserve = self.settings.contract_reserve(conversation.contractor_profile)
            self.settings.require_models_priced(self._contract_profile_models(conversation.contractor_profile))
            repo.assert_can_start_paid_operation(user_id, reserve)
            snapshot_path = self._generation_snapshot_path(user_id, conversation_id, "regeneration")
            self.artifacts.write_json(snapshot_path.relative_to(self.artifacts.data_root), snapshot)
            attempt = repo.create_generation_attempt(
                user_id,
                conversation_id,
                kind="full_regeneration",
                contractor_profile=conversation.contractor_profile,
                request_snapshot_path=snapshot_path,
                source_manifest=manifest,
                base_contract_version_id=base.contract_version_id,
                base_draft_revision=draft.draft_revision if include_draft else None,
                idempotency_key=idempotency_key,
                expected_credit=reserve,
            )
            return QueuedGeneration(
                conversation_id,
                attempt.generation_attempt_id,
                attempt.status,
                attempt.attempt_number,
            )

    async def execute_generation_attempt(self, generation_attempt_id: str) -> GeneratedContract:
        """按 durable checkpoint 执行 Contract 流水线，中断后复用原 thread 续跑。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            attempt = session.get(ContractGenerationAttemptRecord, generation_attempt_id)
            if attempt is None:
                raise ContractWorkflowError("Contract generation attempt 不存在")
            user_id = attempt.owner_user_id
            conversation_id = attempt.conversation_id
            snapshot = json.loads(self.artifacts.read_text(attempt.request_snapshot_path))
            conversation = repo.get_conversation(user_id, conversation_id)
            contract_language = normalize_language(
                snapshot.get("contract_language"),
                default=conversation.contract_language,
            )
            repo.ensure_generation_stages(generation_attempt_id)
            if attempt.status == "queued":
                attempt.status = "running"
                attempt.started_at = utc_now()
            repo.set_generation_attempt_status(
                generation_attempt_id,
                "running",
                event_type="contractor_started",
                event_message="Contractor 已开始 Stage 1：Effective Problem Brief 与 Mathematical Diagnosis",
            )

        async with self._lock(conversation_id):
            try:
                sources = self._source_context(
                    list(snapshot.get("source_manifest") or []), generation_attempt_id
                )
                profile = str(snapshot.get("contractor_profile") or "high")
                scope_id = generation_attempt_id

                stage1 = self._generation_stage_state(
                    generation_attempt_id, "contractor_stage1"
                )
                diagnosis = self._completed_stage_text(stage1)
                if diagnosis is None:
                    diagnosis, diagnosis_turn = await self._validated_diagnosis_turn(
                        scope_id=scope_id,
                        task=self._resume_stage_task(
                            self._contractor_stage1_task(snapshot, sources), stage1
                        ),
                        profile=profile,
                        provider_thread_id=stage1.get("provider_thread_id"),
                        generation_attempt_id=generation_attempt_id,
                        stage_key="contractor_stage1",
                        output_language=contract_language,
                    )
                    diagnosis_path = self._persist_generation_stage(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        generation_attempt_id=generation_attempt_id,
                        stage_key="contractor_stage1",
                        role="contractor_stage_1",
                        text=diagnosis,
                        turn=diagnosis_turn,
                    )
                    with self.database.session() as session:
                        repo = RepositoryHub(session, self.artifacts)
                        repo.append_event(
                            "contract_generation",
                            generation_attempt_id,
                            "contractor_stage1_completed",
                            "Contractor 已完成内部数学诊断",
                            {
                                "diagnosis_artifact_path": self.artifacts.reference(
                                    diagnosis_path
                                )
                            },
                        )
                        repo.append_event(
                            "contract_generation",
                            generation_attempt_id,
                            "contractor_stage2_started",
                            "Contractor 已进入 Stage 2：撰写 standalone Research Contract",
                        )

                stage1 = self._generation_stage_state(
                    generation_attempt_id, "contractor_stage1"
                )
                stage2 = self._generation_stage_state(
                    generation_attempt_id, "contractor_stage2"
                )
                candidate_text = self._completed_stage_text(stage2)
                if candidate_text is None:
                    candidate, draft_turn = await self._validated_contract_turn(
                        role="contractor",
                        scope_id=scope_id,
                        task=self._resume_stage_task(
                            self._contractor_stage2_task(), stage2
                        ),
                        profile=profile,
                        persistent=True,
                        provider_thread_id=(
                            stage2.get("provider_thread_id")
                            or stage1.get("provider_thread_id")
                        ),
                        generation_attempt_id=generation_attempt_id,
                        stage_key="contractor_stage2",
                        output_language=contract_language,
                    )
                    self._persist_generation_stage(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        generation_attempt_id=generation_attempt_id,
                        stage_key="contractor_stage2",
                        role="contractor_stage_2",
                        text=candidate.markdown,
                        turn=draft_turn,
                    )
                    with self.database.session() as session:
                        RepositoryHub(session, self.artifacts).append_event(
                            "contract_generation",
                            generation_attempt_id,
                            "contractor_completed",
                            "Contractor 已完成 Stage 2 并提交结构合格的 standalone Contract 候选",
                        )
                else:
                    candidate = parse_contract(candidate_text)
                    validation = validate_contract(candidate)
                    if not validation.ok:
                        raise ContractWorkflowError(validation.repair_message())

                integrated, warnings = await self._audit_and_integrate(
                    conversation_id,
                    user_id=user_id,
                    generation_attempt_id=generation_attempt_id,
                    snapshot=snapshot,
                    source_context=sources,
                    diagnosis=diagnosis,
                    candidate=candidate,
                    profile=profile,
                    contract_language=contract_language,
                )
                execution_policy = dict(snapshot.get("execution_policy") or {}) or self._default_execution_policy(user_id)
                result = self._save_generated_contract(
                    user_id,
                    conversation_id,
                    generation_attempt_id,
                    integrated,
                    list(snapshot.get("source_manifest") or []),
                    execution_policy=execution_policy,
                    created_by="contractor" if snapshot.get("kind") == "initial" else "full_regeneration",
                    warnings=warnings,
                )
                return result
            except asyncio.CancelledError:
                self._interrupt_generation_attempt(
                    generation_attempt_id,
                    "Runner shutdown interrupted Contract generation",
                )
                raise
            except ContractGenerationInterrupted as exc:
                self._interrupt_generation_attempt(generation_attempt_id, str(exc))
                raise
            except (
                CodexExecError,
                ContractAppServerError,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as exc:
                self._interrupt_generation_attempt(generation_attempt_id, str(exc))
                raise ContractGenerationInterrupted(str(exc)) from exc
            except Exception as exc:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).set_generation_attempt_status(
                        generation_attempt_id,
                        "failed",
                        error=str(exc),
                        event_type="contract_generation_failed",
                        event_message="Research Contract 生成失败",
                        event_payload={"error": str(exc)},
                    )
                raise

    async def generate_initial(
        self,
        user_id: str,
        conversation_id: str,
        *,
        profile: str = "high",
    ) -> GeneratedContract:
        """Compatibility helper for tests/local probes; product HTTP uses the queue."""
        with self.database.session() as session:
            conversation = RepositoryHub(session, self.artifacts).get_conversation(user_id, conversation_id)
            conversation.contractor_profile = profile
        queued = self.queue_initial_generation(
            user_id,
            conversation_id,
            idempotency_key=f"sync-{conversation_id}-{new_id('idempotency')}",
        )
        return await self.execute_generation_attempt(queued.generation_attempt_id)

    # ------------------------------------------------------------------
    # Working Draft / structured Execution Policy
    # ------------------------------------------------------------------
    def start_working_draft(
        self,
        user_id: str,
        conversation_id: str,
        *,
        base_version: int | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            if conversation.working_draft is not None:
                return self._draft_payload(conversation.working_draft)
            latest = repo.get_contract_version(user_id, conversation_id, base_version)
            policy = (
                dict(conversation.execution_policy or {})
                or dict(latest.execution_policy or {})
                or self._default_execution_policy(user_id)
            )
            draft = repo.save_working_draft(
                conversation,
                base_version=latest.version,
                title=latest.title,
                sections=dict(latest.sections),
                source_manifest=list(latest.source_manifest),
                execution_policy=policy,
                expected_revision=0,
            )
            repo.append_event(
                "conversation",
                conversation_id,
                "working_draft_created",
                f"从 Contract v{latest.version} 建立 Working Draft",
                {"draft_revision": draft.draft_revision},
            )
            return self._draft_payload(draft)

    def patch_working_draft_section(
        self,
        user_id: str,
        conversation_id: str,
        section_key: str,
        content: str,
        *,
        expected_revision: int | None = None,
        base_version: int | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            draft = conversation.working_draft
            if draft is None:
                if expected_revision not in (None, 0):
                    raise ContractWorkflowError("DraftRevisionConflict")
                base = repo.get_contract_version(user_id, conversation_id, base_version)
                document = ContractDocument(base.title, dict(base.sections), "")
                updated = document.with_section(section_key, content)
                structure_errors = validate_fragment_structure(section_key, updated.sections[section_key])
                if structure_errors:
                    raise ContractWorkflowError("\n".join(structure_errors))
                draft = repo.save_working_draft(
                    conversation,
                    base_version=base.version,
                    title=updated.title,
                    sections=updated.sections,
                    source_manifest=list(base.source_manifest),
                    execution_policy=dict(conversation.execution_policy or {})
                    or self._default_execution_policy(user_id),
                    expected_revision=0,
                )
            else:
                if base_version is not None and int(draft.base_version) != int(base_version):
                    raise ContractWorkflowError("ExistingDraftBaseConflict")
                document = ContractDocument(draft.title, dict(draft.sections), "")
                updated = document.with_section(section_key, content)
                structure_errors = validate_fragment_structure(section_key, updated.sections[section_key])
                if structure_errors:
                    raise ContractWorkflowError("\n".join(structure_errors))
                draft = repo.save_working_draft(
                    conversation,
                    base_version=draft.base_version,
                    title=updated.title,
                    sections=updated.sections,
                    source_manifest=list(draft.source_manifest),
                    execution_policy=dict(draft.execution_policy),
                    expected_revision=expected_revision,
                )
            validation = validate_contract(updated)
            repo.append_event(
                "conversation",
                conversation_id,
                "working_draft_autosaved",
                f"Working Draft 的 `{section_key}` 已暂存",
                {"draft_revision": draft.draft_revision, "section_key": section_key},
            )
            return self._draft_payload(draft, validation=validation)

    def patch_working_draft_fragment(
        self,
        user_id: str,
        conversation_id: str,
        section_key: str,
        fragment_id: str,
        title: str,
        content: str,
        *,
        expected_revision: int | None = None,
        base_version: int | None = None,
    ) -> dict[str, Any]:
        """按稳定 ID 更新一个 D/E 卡片，服务端保留 ID 并只替换标题与正文。"""
        if section_key not in {"detailed_horizon", "structured_horizon"}:
            raise ContractWorkflowError("只有 D/E Section 支持 Fragment 编辑")
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            draft = conversation.working_draft
            if draft is None:
                if expected_revision not in (None, 0):
                    raise ContractWorkflowError("DraftRevisionConflict")
                base = repo.get_contract_version(user_id, conversation_id, base_version)
                document = ContractDocument(base.title, dict(base.sections), "")
                source_manifest = list(base.source_manifest)
                execution_policy = (
                    dict(conversation.execution_policy or {})
                    or self._default_execution_policy(user_id)
                )
                selected_base_version = base.version
                save_revision = 0
            else:
                if base_version is not None and int(draft.base_version) != int(base_version):
                    raise ContractWorkflowError("ExistingDraftBaseConflict")
                document = ContractDocument(draft.title, dict(draft.sections), "")
                source_manifest = list(draft.source_manifest)
                execution_policy = dict(draft.execution_policy)
                selected_base_version = draft.base_version
                save_revision = expected_revision

            source = document.sections.get(section_key, "")
            if len([item for item in contract_fragments(section_key, source) if item.fragment_id == fragment_id.strip().upper()]) != 1:
                raise ContractWorkflowError(f"Fragment `{fragment_id}` 不存在或不唯一")
            try:
                updated_source = replace_contract_fragment(
                    section_key, source, fragment_id, title, content
                )
            except ValueError as exc:
                raise ContractWorkflowError(str(exc)) from exc
            structure_errors = validate_fragment_structure(section_key, updated_source)
            if structure_errors:
                raise ContractWorkflowError("\n".join(structure_errors))
            updated = document.with_section(section_key, updated_source)
            draft = repo.save_working_draft(
                conversation,
                base_version=selected_base_version,
                title=updated.title,
                sections=updated.sections,
                source_manifest=source_manifest,
                execution_policy=execution_policy,
                expected_revision=save_revision,
            )
            validation = validate_contract(updated)
            repo.append_event(
                "conversation",
                conversation_id,
                "working_draft_fragment_autosaved",
                f"Working Draft 的 `{fragment_id.strip().upper()}` 已暂存",
                {
                    "draft_revision": draft.draft_revision,
                    "section_key": section_key,
                    "fragment_id": fragment_id.strip().upper(),
                },
            )
            return self._draft_payload(draft, validation=validation)

    def patch_execution_policy(
        self,
        user_id: str,
        conversation_id: str,
        *,
        model_tier: str,
        duration_tier: str,
    ) -> dict[str, Any]:
        """更新对话级执行档位，不创建或修改 Working Draft。"""
        policy = self.settings.make_execution_policy(model_tier, duration_tier)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            repo.update_conversation_execution_policy(
                user_id, conversation_id, policy
            )
            repo.append_event(
                "conversation",
                conversation_id,
                "execution_policy_updated",
                "Execution Policy and Budget 已更新",
                {
                    "model_tier": policy["model_tier"],
                    "duration_tier": policy["duration_tier"],
                },
            )
            return dict(policy)

    def commit_working_draft(self, user_id: str, conversation_id: str) -> GeneratedContract:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            draft = conversation.working_draft
            if draft is None:
                raise ContractWorkflowError("当前没有 Working Draft")
            document = ContractDocument(draft.title, dict(draft.sections), render_contract(draft.title, dict(draft.sections)))
            validation = validate_contract(document)
            if not validation.ok:
                raise ContractWorkflowError(validation.repair_message())
            markdown = document.markdown
            path = self._contract_path(user_id, conversation_id, conversation.latest_contract_version + 1)
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), markdown)
            version = repo.create_contract_version(
                conversation,
                title=document.title,
                markdown_path=path,
                sections=document.sections,
                source_manifest=list(draft.source_manifest),
                execution_policy=dict(conversation.execution_policy or {})
                or self._default_execution_policy(user_id),
                created_by="manual_section_edit",
            )
            repo.append_event(
                "conversation",
                conversation_id,
                "contract_version_created",
                f"Working Draft 已冻结为 Contract v{version.version}",
                {"version": version.version},
            )
            return GeneratedContract(
                conversation_id,
                version.version,
                version.contract_version_id,
                markdown,
                validation.warnings,
                dict(version.execution_policy),
            )

    def discard_working_draft(self, user_id: str, conversation_id: str) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.discard_working_draft(user_id, conversation_id)
            repo.append_event("conversation", conversation_id, "working_draft_discarded", "Working Draft 已放弃")

    # ------------------------------------------------------------------
    # Contract Chat Assistant
    # ------------------------------------------------------------------
    async def contract_chat(
        self,
        user_id: str,
        conversation_id: str,
        message: str,
        *,
        expected_revision: int | None = None,
        profile: str | None = None,
        allow_draft_changes: bool | None = None,
        selected_version: int | None = None,
        expected_draft_revision: int | None = None,
        on_stream_event: ChatStreamEvent | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """先原子预扣 Credit，再执行并在任意终态结算差额。"""
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            selected = repo.get_contract_version(user_id, conversation_id, selected_version)
            frozen_profile = conversation.contractor_profile
            self.settings.require_models_priced(self._contract_profile_models(frozen_profile))
            call = repo.create_contract_chat_call(
                user_id, conversation_id, selected.contract_version_id,
                profile=frozen_profile,
                expected_credit=self.settings.contract_reserve(frozen_profile, chat=True),
                idempotency_key=idempotency_key or new_id("chat-idempotency"),
            )
            if call.status not in {"reserved", "running"}:
                raise ContractWorkflowError("该 Contract Chat 幂等请求已经结束")
            call.status = "running"
            chat_call_id = call.chat_call_id
        try:
            result = await self._contract_chat_reserved(
                user_id, conversation_id, message,
                expected_revision=expected_revision, profile=profile,
                allow_draft_changes=allow_draft_changes,
                selected_version=selected_version,
                expected_draft_revision=expected_draft_revision,
                on_stream_event=on_stream_event,
                chat_call_id=chat_call_id,
            )
        except BaseException as exc:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).settle_contract_chat_call(
                    chat_call_id, status="failed", error=str(exc)
                )
            raise
        with self.database.session() as session:
            actual = RepositoryHub(session, self.artifacts).settle_contract_chat_call(
                chat_call_id, status="completed"
            )
        return {**result, "chat_call_id": chat_call_id, "actual_credit": actual}

    async def _contract_chat_reserved(
        self,
        user_id: str,
        conversation_id: str,
        message: str,
        *,
        expected_revision: int | None = None,
        profile: str | None = None,
        allow_draft_changes: bool | None = None,
        selected_version: int | None = None,
        expected_draft_revision: int | None = None,
        on_stream_event: ChatStreamEvent | None = None,
        chat_call_id: str,
    ) -> dict[str, Any]:
        """Run the compact Contract Chat assistant.

        A pure question must not create or mutate a Working Draft.  A Draft is
        created lazily only when the assistant returns an explicit, validated
        A--F patch and the caller allowed changes.  This keeps the visible chat
        cheap while preserving optimistic concurrency with the manual editor.
        """
        if expected_draft_revision is not None:
            expected_revision = expected_draft_revision
        changes_allowed = bool(allow_draft_changes)
        if not message.strip():
            raise ContractWorkflowError("Contract Chat 消息不能为空")

        async with self._lock(conversation_id):
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                conversation = repo.get_conversation(user_id, conversation_id)
                self._ensure_editable(conversation)
                if not conversation.latest_contract_version:
                    raise ContractWorkflowError("首版 Contract 尚未生成")

                selected = repo.get_contract_version(user_id, conversation_id, selected_version)
                draft = conversation.working_draft
                observed_draft_revision = int(draft.draft_revision) if draft is not None else None
                observed_draft_base_version = int(draft.base_version) if draft is not None else None
                draft_compatible = draft is not None and int(draft.base_version) == int(selected.version)
                use_draft = draft is not None and (selected_version is None or draft_compatible)
                draft_existed = use_draft
                if use_draft:
                    assert draft is not None
                    before_revision = int(draft.draft_revision)
                    if expected_revision is not None and before_revision != int(expected_revision):
                        raise ContractWorkflowError("DraftRevisionConflict")
                    context_title = draft.title
                    context_sections = dict(draft.sections)
                    base_version = int(draft.base_version)
                    source_manifest = list(draft.source_manifest)
                else:
                    if expected_revision not in (None, 0):
                        raise ContractWorkflowError("DraftRevisionConflict")
                    before_revision = 0
                    context_title = selected.title
                    context_sections = dict(selected.sections)
                    base_version = int(selected.version)
                    source_manifest = list(selected.source_manifest)

                context = type(
                    "ContractChatContext",
                    (),
                    {
                        "title": context_title,
                        "sections": context_sections,
                        "draft_revision": before_revision,
                    },
                )()
                repo.append_contract_chat_message(
                    user_id,
                    conversation_id,
                    contract_version_id=selected.contract_version_id,
                    role="user",
                    content=message,
                    draft_revision_before=before_revision,
                    draft_revision_after=before_revision,
                )
                history = [
                    {"role": item.role, "content": item.content}
                    for item in repo.list_contract_chat_messages(
                        user_id, conversation_id, selected.contract_version_id
                    )
                ]
                thread = repo.get_agent_thread(
                    "contract_version", selected.contract_version_id, "contract_chat_assistant"
                )
                provider_thread_id = thread.provider_thread_id if thread else None
                task = self._contract_chat_task(
                    conversation,
                    context,
                    history,
                    message,
                    changes_allowed=changes_allowed,
                )
                # Contract Chat follows the profile frozen on its Conversation;
                # callers cannot silently route one branch through another tier.
                profile_name = conversation.contractor_profile
                contract_language = normalize_language(
                    selected.contract_language,
                    default=conversation.contract_language,
                )

            response, patches, turn = await self._validated_chat_turn(
                selected.contract_version_id,
                user_id=user_id,
                conversation_id=conversation_id,
                chat_call_id=chat_call_id,
                task=task,
                profile=profile_name,
                provider_thread_id=provider_thread_id,
                on_stream_event=on_stream_event,
                output_language=contract_language,
            )

            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                conversation = repo.get_conversation(user_id, conversation_id)
                current_draft = conversation.working_draft

                # The draft state observed before the model call is part of the
                # optimistic-concurrency contract.  A simultaneous manual edit
                # or assistant turn must never be silently overwritten.
                if draft_existed:
                    if current_draft is None or int(current_draft.draft_revision) != before_revision:
                        raise ContractWorkflowError("DraftRevisionConflict")
                elif observed_draft_revision is not None:
                    # A historical-version discussion may intentionally ignore an
                    # unrelated Working Draft.  It is safe only while that Draft
                    # remains byte-for-byte at the revision observed before the
                    # model call; it must not be patched or silently replaced.
                    if (
                        current_draft is None
                        or int(current_draft.draft_revision) != observed_draft_revision
                        or int(current_draft.base_version) != observed_draft_base_version
                    ):
                        raise ContractWorkflowError("DraftRevisionConflict")
                elif current_draft is not None:
                    raise ContractWorkflowError("DraftRevisionConflict")

                if patches and not changes_allowed:
                    patches = {}
                    response = response.rstrip() + "\n\n本轮未启用 Working Draft 修改，因此没有应用任何 Section patch。"

                validation: ContractValidation | None = None
                if patches:
                    if conversation.working_draft is not None and not draft_compatible and not use_draft:
                        raise ContractWorkflowError(
                            "ExistingDraftBaseConflict:当前 Working Draft 基于另一个 Contract Version；"
                            "请先保存或放弃现有草稿，再修改当前历史版本"
                        )
                    document = ContractDocument(context_title, dict(context_sections), "")
                    for key, value in patches.items():
                        document = document.with_section(key, value)
                    validation = validate_contract(document)
                    if not validation.ok:
                        raise ContractWorkflowError(validation.repair_message())
                    current_draft = repo.save_working_draft(
                        conversation,
                        base_version=base_version,
                        title=document.title,
                        sections=document.sections,
                        source_manifest=source_manifest,
                        execution_policy=dict(conversation.execution_policy or {}),
                        expected_revision=before_revision,
                    )

                after_revision = int(current_draft.draft_revision) if current_draft is not None else 0
                repo.append_contract_chat_message(
                    user_id,
                    conversation_id,
                    contract_version_id=selected.contract_version_id,
                    role="assistant",
                    content=response,
                    draft_revision_before=before_revision,
                    draft_revision_after=after_revision,
                )
                if turn.thread_id:
                    repo.save_agent_thread(
                        scope_type="contract_version",
                        scope_id=selected.contract_version_id,
                        role="contract_chat_assistant",
                        provider_thread_id=turn.thread_id,
                        persistent=True,
                        model=turn.model,
                        reasoning_effort=turn.reasoning_effort,
                    )
                return {
                    "reply": response,
                    "contract_version_id": selected.contract_version_id,
                    "contract_version": selected.version,
                    "patches": patches,
                    "draft_changed": bool(patches),
                    "draft_revision_before": before_revision,
                    "draft_revision_after": after_revision,
                    "changed_section_keys": sorted(patches),
                    "draft": self._draft_payload(current_draft, validation=validation)
                    if current_draft is not None
                    else None,
                }

    async def revise_from_message(
        self, user_id: str, conversation_id: str, message: str, *, profile: str = "high"
    ) -> dict[str, Any]:
        """Backward-compatible alias for the dedicated Contract Chat assistant."""
        with self.database.session() as session:
            conversation = RepositoryHub(session, self.artifacts).get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
        return await self.contract_chat(
            user_id, conversation_id, message, profile=profile
        )

    # ------------------------------------------------------------------
    # Clone / approve / Run handoff
    # ------------------------------------------------------------------
    def clone_from_contract(self, user_id: str, conversation_id: str, version: int) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            source_conversation = repo.get_conversation(user_id, conversation_id)
            contract = repo.get_contract_version(user_id, conversation_id, version)
            referenced_ids = [
                str(item.get("file_id"))
                for item in contract.source_manifest
                if item.get("file_id")
            ]
            current_manifest = repo.source_manifest(user_id, referenced_ids)
            file_ids = [
                str(item.get("file_id"))
                for item in current_manifest
                if item.get("file_id") and item.get("availability") == "available"
            ]
            new_conversation = repo.create_conversation(
                user_id,
                source_conversation.research_request,
                project_id=source_conversation.project_id,
                display_name=f"{source_conversation.display_name} · 分支",
                source_file_ids=file_ids,
                research_problem=source_conversation.research_problem,
                background_and_prior_progress=source_conversation.background_and_prior_progress,
                method_preferences_and_restrictions=source_conversation.method_preferences_and_restrictions,
                valuable_partial_outcomes=source_conversation.valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=source_conversation.possible_ideas_and_rough_approaches,
                contractor_profile=source_conversation.contractor_profile,
                contract_language=contract.contract_language,
                execution_policy=dict(source_conversation.execution_policy or {})
                or dict(contract.execution_policy or {})
                or self._default_execution_policy(user_id),
                status="ready",
            )
            markdown = self.artifacts.read_text(contract.markdown_path)
            path = self._contract_path(user_id, new_conversation.conversation_id, 1)
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), markdown)
            missing = [item.get("original_name") for item in current_manifest if item.get("availability") != "available"]
            version_record = repo.create_contract_version(
                new_conversation,
                title=contract.title,
                markdown_path=path,
                sections=dict(contract.sections),
                source_manifest=current_manifest,
                execution_policy=dict(new_conversation.execution_policy or {}),
                created_by="contract_fork",
            )
            repo.append_event(
                "conversation",
                new_conversation.conversation_id,
                "contract_cloned",
                f"从 Contract v{version} 创建新对话",
                {
                    "source_conversation_id": conversation_id,
                    "source_contract_version": version,
                },
            )
            return {
                "conversation_id": new_conversation.conversation_id,
                "version": version_record.version,
                "missing_sources": missing,
            }

    def approve(
        self,
        user_id: str,
        conversation_id: str,
        *,
        target_kind: str,
        version: int | None = None,
        draft_revision: int | None = None,
        research_language: str | None = None,
    ) -> dict[str, Any]:
        if target_kind not in {"immutable_version", "working_draft"}:
            raise ContractWorkflowError("approve target_kind 无效")
        if target_kind == "working_draft":
            with self.database.session() as session:
                conversation = RepositoryHub(session, self.artifacts).get_conversation(user_id, conversation_id)
                draft = conversation.working_draft
                if draft is None:
                    raise ContractWorkflowError("当前没有 Working Draft")
                if draft_revision is not None and int(draft.draft_revision) != int(draft_revision):
                    raise ContractWorkflowError("DraftRevisionConflict")
            committed = self.commit_working_draft(user_id, conversation_id)
            version = committed.version
        return self._approve_version(
            user_id, conversation_id, version, research_language=research_language
        )

    def approve_contract(
        self,
        user_id: str,
        conversation_id: str,
        *,
        target_kind: str,
        version: int | None = None,
        expected_draft_revision: int | None = None,
        acknowledge_historical_with_draft: bool = False,
        research_language: str | None = None,
    ) -> dict[str, Any]:
        if target_kind == "immutable_version":
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                conversation = repo.get_conversation(user_id, conversation_id)
                draft = conversation.working_draft
                selected = version or conversation.latest_contract_version
                if draft is not None and not acknowledge_historical_with_draft:
                    raise ContractWorkflowError(
                        "WorkingDraftApprovalWarning:"
                        f"当前存在基于 Contract v{draft.base_version} 的 Working Draft；"
                        f"你正在批准 immutable Contract v{selected}，草稿不会包含在本次执行中"
                    )
        return self.approve(
            user_id,
            conversation_id,
            target_kind=target_kind,
            version=version,
            draft_revision=expected_draft_revision,
            research_language=research_language,
        )

    def approve_latest(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        """Compatibility alias: approve the latest immutable version."""
        return self._approve_version(user_id, conversation_id, None)

    def _approve_version(
        self,
        user_id: str,
        conversation_id: str,
        version: int | None,
        *,
        research_language: str | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            self._ensure_editable(conversation)
            contract = repo.get_contract_version(user_id, conversation_id, version)
            markdown = self.artifacts.read_text(contract.markdown_path)
            document = parse_contract(markdown)
            validation = validate_contract(document)
            if not validation.ok:
                raise ContractWorkflowError(validation.repair_message())
            missing = [
                item.get("original_name")
                for item in contract.source_manifest
                if item.get("availability") != "available"
                or not item.get("artifact_path")
                or not self.artifacts.resolve(str(item.get("artifact_path"))).is_file()
            ]
            if missing:
                raise ContractWorkflowError("批准失败：源文件已缺失：" + "、".join(str(x) for x in missing))
            policy = dict(conversation.execution_policy or {}) or self._default_execution_policy(user_id)
            normalized_policy = self.settings.make_execution_policy(
                str(policy.get("model_tier") or ""),
                str(policy.get("duration_tier") or ""),
                require_live_mapping=not self.settings.offline_agents,
            )
            normalized_policy = self._resolve_execution_models(normalized_policy)
            self.settings.require_models_priced([
                str(item.get("model") or "")
                for item in dict(normalized_policy.get("resolved_role_models") or {}).values()
            ])
            expected_cost = float(normalized_policy["estimated_cost"])
            repo.assert_can_start_run(user_id, expected_cost)
            run_id = new_id("run")
            user = repo.get_user(user_id)
            frozen_research_language = normalize_language(
                research_language, default=user.default_research_language
            )
            if not user.storage_key:
                raise ContractWorkflowError("当前用户缺少 storage_key")
            run_root = self.artifacts.user_run_root(user.storage_key, run_id)
            run_root.mkdir(parents=True, exist_ok=False)
            contract_snapshot = run_root / "input" / "research_contract.md"
            self.artifacts.write_text(contract_snapshot.relative_to(self.artifacts.data_root), markdown)
            source_dir, frozen_manifest = self.artifacts.materialize_run_sources(run_root, contract.source_manifest)
            initial_nodes = [node.__dict__ for node in extract_initial_nodes(document)]
            handoff = {
                "schema_version": 7,
                "run_id": run_id,
                "owner_user_id": user_id,
                "conversation_id": conversation_id,
                "contract_version_id": contract.contract_version_id,
                "contract_version": contract.version,
                "contract_sha256": contract.markdown_sha256,
                "contract_language": normalize_language(contract.contract_language),
                "research_language": frozen_research_language,
                "contract_snapshot_path": self.artifacts.reference(contract_snapshot),
                "source_snapshot_path": self.artifacts.reference(source_dir),
                "source_manifest": frozen_manifest,
                "initial_nodes": initial_nodes,
                "graph_relations": ["requires", "informs", "alternative_to", "conditional_on"],
                "execution_policy": normalized_policy,
                "expected_cost": expected_cost,
            }
            handoff_path = run_root / "input" / "prover_handoff.json"
            self.artifacts.write_json(handoff_path.relative_to(self.artifacts.data_root), handoff)
            run, attempt = repo.create_research_run(
                conversation,
                contract,
                run_id=run_id,
                run_root=run_root,
                contract_snapshot_path=contract_snapshot,
                source_snapshot_path=source_dir,
                handoff_manifest_path=handoff_path,
                expected_cost=expected_cost,
                execution_policy=normalized_policy,
                research_language=frozen_research_language,
            )
            repo.append_event(
                "conversation",
                conversation_id,
                "contract_approved",
                f"Contract v{contract.version} 已批准并锁定",
                {
                    "run_id": run.run_id,
                    "execution_policy": normalized_policy,
                    "research_language": frozen_research_language,
                },
            )
            repo.append_event(
                "run",
                run.run_id,
                "run_shell_created",
                "Research Run shell 与 Prover Handoff 已建立",
                {"attempt_id": attempt.attempt_id},
            )
            return {
                "run_id": run.run_id,
                "attempt_id": attempt.attempt_id,
                "status": run.status,
                "expected_cost": expected_cost,
                "execution_policy": normalized_policy,
                "research_language": frozen_research_language,
                "handoff_manifest_path": self.artifacts.reference(handoff_path),
                "initial_nodes": initial_nodes,
            }

    # ------------------------------------------------------------------
    # Agent pipeline helpers
    # ------------------------------------------------------------------
    async def _audit_and_integrate(
        self,
        conversation_id: str,
        *,
        user_id: str,
        generation_attempt_id: str,
        snapshot: dict[str, Any],
        source_context: str,
        diagnosis: str,
        candidate: ContractDocument,
        profile: str,
        contract_language: str,
    ) -> tuple[ContractDocument, list[str]]:
        audit_specs = (
            ("contract_fidelity_auditor", "auditor_fidelity"),
            ("contract_completeness_auditor", "auditor_completeness"),
            ("contract_map_auditor", "auditor_map"),
        )
        audit_task = self._audit_task(snapshot, source_context, diagnosis, candidate.markdown)
        reports: dict[str, str] = {}
        pending: list[tuple[str, str, dict[str, Any]]] = []
        for role, stage_key in audit_specs:
            stage = self._generation_stage_state(generation_attempt_id, stage_key)
            text = self._completed_stage_text(stage)
            if text is not None:
                errors = self._validate_audit(text)
                if errors:
                    raise ContractWorkflowError("\n".join(errors))
                reports[role] = text
            else:
                pending.append((role, stage_key, stage))

        if pending:
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                for role, _, _ in pending:
                    repo.append_event(
                        "contract_generation",
                        generation_attempt_id,
                        f"{role}_started",
                        f"{role} 已开始审查",
                    )
            results = await asyncio.gather(
                *(
                    self._run_auditor_checkpoint(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        generation_attempt_id=generation_attempt_id,
                        role=role,
                        stage_key=stage_key,
                        stage=stage,
                        task=audit_task,
                        profile=profile,
                        output_language=contract_language,
                    )
                    for role, stage_key, stage in pending
                ),
                return_exceptions=True,
            )
            for (role, _, _), result in zip(pending, results, strict=True):
                if isinstance(result, BaseException):
                    raise result
                reports[role] = result

        reports_markdown = "\n\n".join(
            f"# {role}\n\n{reports[role]}" for role, _ in audit_specs
        )
        integration_task = self._integration_task(
            snapshot, source_context, diagnosis, candidate.markdown, reports_markdown
        )
        stage = self._generation_stage_state(generation_attempt_id, "integrator")
        integrated_text = self._completed_stage_text(stage)
        if integrated_text is None:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).append_event(
                    "contract_generation",
                    generation_attempt_id,
                    "integrator_started",
                    "Contract Integrator 已开始合并审计意见",
                )
            integrated, integration_turn = await self._validated_contract_turn(
                role="contract_integrator",
                scope_id=generation_attempt_id,
                task=self._resume_stage_task(integration_task, stage),
                profile=profile,
                persistent=True,
                provider_thread_id=stage.get("provider_thread_id"),
                generation_attempt_id=generation_attempt_id,
                stage_key="integrator",
                output_language=contract_language,
            )
            self._persist_generation_stage(
                user_id=user_id,
                conversation_id=conversation_id,
                generation_attempt_id=generation_attempt_id,
                stage_key="integrator",
                role="contract_integrator",
                text=integrated.markdown,
                turn=integration_turn,
            )
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).append_event(
                    "contract_generation",
                    generation_attempt_id,
                    "integrator_completed",
                    "Contract Integrator 已完成",
                )
        else:
            integrated = parse_contract(integrated_text)
            validation = validate_contract(integrated)
            if not validation.ok:
                raise ContractWorkflowError(validation.repair_message())
        validation = validate_contract(integrated)
        return integrated, validation.warnings

    async def _run_auditor_checkpoint(
        self,
        *,
        user_id: str,
        conversation_id: str,
        generation_attempt_id: str,
        role: str,
        stage_key: str,
        stage: dict[str, Any],
        task: str,
        profile: str,
        output_language: str,
    ) -> str:
        turn = await self._run_simple_role(
            role,
            scope_id=generation_attempt_id,
            task=self._resume_stage_task(task, stage),
            profile=profile,
            persistent=True,
            provider_thread_id=stage.get("provider_thread_id"),
            generation_attempt_id=generation_attempt_id,
            stage_key=stage_key,
            output_language=output_language,
        )
        self._persist_generation_stage(
            user_id=user_id,
            conversation_id=conversation_id,
            generation_attempt_id=generation_attempt_id,
            stage_key=stage_key,
            role=role,
            text=turn.text,
            turn=turn,
        )
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).append_event(
                "contract_generation",
                generation_attempt_id,
                f"{role}_completed",
                f"{role} 已完成审查",
            )
        return turn.text

    async def _validated_diagnosis_turn(
        self,
        *,
        scope_id: str,
        task: str,
        profile: str,
        provider_thread_id: str | None,
        generation_attempt_id: str,
        stage_key: str,
        output_language: str = "zh",
    ) -> tuple[str, AgentTurn]:
        """Run Stage 1 in the Contractor thread with a minimal internal-artifact gate."""
        current_task = task
        current_thread = provider_thread_id
        last_turn: AgentTurn | None = None
        stage = self._generation_stage_state(generation_attempt_id, stage_key)
        start_repair = int(stage.get("repair_number") or 0)
        for repair in range(start_repair, self.settings.contract_repair_attempts + 1):
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).begin_generation_stage(
                    generation_attempt_id,
                    stage_key,
                    repair_number=repair,
                    provider_thread_id=current_thread,
                )
            if repair:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).set_generation_attempt_status(
                        generation_attempt_id,
                        "repairing",
                        event_type="contractor_stage1_repairing",
                        event_message=f"Contractor Stage 1 正在执行第 {repair} 次结构修复",
                    )
            turn = await self.backend.run_role(
                role="contractor",
                scope_id=scope_id,
                task=current_task,
                profile=profile,
                persistent=True,
                provider_thread_id=current_thread,
                repair_number=repair,
                on_thread_ready=self._generation_thread_callback(
                    generation_attempt_id, stage_key
                ),
                runtime_root=self._generation_runtime_root(generation_attempt_id),
                output_language=output_language,
            )
            self._record_generation_turn(generation_attempt_id, "contractor_stage_1", turn)
            last_turn = turn
            current_thread = turn.thread_id or current_thread
            if current_thread:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).bind_generation_stage_thread(
                        generation_attempt_id, stage_key, current_thread
                    )
            text = self._normalize_markdown(turn.text).strip()
            errors: list[str] = []
            if turn.status != "completed":
                raise ContractGenerationInterrupted(
                    f"Contractor Stage 1 transport 调用失败：{turn.error or turn.status}"
                )
            if len(text) < 500:
                errors.append("Stage 1 数学诊断过短，尚不足以支撑后续 Contract drafting。")
            if re.search(r"^#\s+Research Contract\s*$", text, flags=re.MULTILINE):
                errors.append("Stage 1 不得提前输出正式 `# Research Contract`；这里只提交内部 Effective Problem Brief 与 Mathematical Diagnosis。")
            if not errors:
                return text + "\n", turn
            current_task = (
                "Stage 1 内部数学诊断未通过最小结构检查。请在同一 thread 中一次性修复：\n"
                + "\n".join(f"- {error}" for error in errors)
                + "\n请重新提交完整的 Effective Problem Brief 与 Mathematical Diagnosis；不要生成正式 Contract。"
            )
        raise ContractWorkflowError(
            "Contractor Stage 1 在格式修复后仍未提交有效数学诊断："
            + (last_turn.error if last_turn and last_turn.error else "diagnosis structure invalid")
        )

    async def _validated_contract_turn(
        self,
        *,
        role: str,
        scope_id: str,
        task: str,
        profile: str,
        persistent: bool,
        provider_thread_id: str | None,
        generation_attempt_id: str | None = None,
        stage_key: str | None = None,
        output_language: str = "zh",
    ) -> tuple[ContractDocument, AgentTurn]:
        current_task = task
        last_turn: AgentTurn | None = None
        current_thread = provider_thread_id
        repair_limit = (
            self.settings.contract_integrator_repair_attempts
            if role == "contract_integrator"
            else self.settings.contract_repair_attempts
        )
        start_repair = 0
        if generation_attempt_id and stage_key:
            start_repair = int(
                self._generation_stage_state(generation_attempt_id, stage_key).get(
                    "repair_number"
                )
                or 0
            )
        for repair in range(start_repair, repair_limit + 1):
            if generation_attempt_id and stage_key:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).begin_generation_stage(
                        generation_attempt_id,
                        stage_key,
                        repair_number=repair,
                        provider_thread_id=current_thread,
                    )
            if repair and generation_attempt_id:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).set_generation_attempt_status(
                        generation_attempt_id,
                        "repairing",
                        event_type=f"{role}_repairing",
                        event_message=f"{role} 正在执行第 {repair} 次聚合式格式修复",
                    )
            turn = await self.backend.run_role(
                role=role,
                scope_id=scope_id,
                task=current_task,
                profile=profile,
                persistent=persistent,
                provider_thread_id=current_thread,
                repair_number=repair,
                on_thread_ready=(
                    self._generation_thread_callback(generation_attempt_id, stage_key)
                    if generation_attempt_id and stage_key
                    else None
                ),
                runtime_root=(
                    self._generation_runtime_root(generation_attempt_id)
                    if generation_attempt_id else None
                ),
                output_language=output_language,
            )
            if generation_attempt_id:
                self._record_generation_turn(generation_attempt_id, role, turn)
            last_turn = turn
            current_thread = turn.thread_id or current_thread
            if current_thread and generation_attempt_id and stage_key:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).bind_generation_stage_thread(
                        generation_attempt_id, stage_key, current_thread
                    )
            if turn.status != "completed":
                raise ContractGenerationInterrupted(
                    f"{role} transport 调用失败：{turn.error or turn.status}"
                )
            document = parse_contract(self._normalize_markdown(turn.text))
            validation = validate_contract(document)
            if validation.ok:
                return document, turn
            current_task = validation.repair_message()
        raise ContractWorkflowError(
            f"{role} 在 {repair_limit} 次格式修复后仍未提交有效 Contract："
            + (last_turn.error if last_turn and last_turn.error else "结构不合格")
        )

    async def _run_simple_role(
        self,
        role: str,
        *,
        scope_id: str,
        task: str,
        profile: str,
        persistent: bool,
        provider_thread_id: str | None = None,
        generation_attempt_id: str | None = None,
        stage_key: str | None = None,
        output_language: str = "zh",
    ) -> AgentTurn:
        current = task
        thread = provider_thread_id
        start_repair = 0
        if generation_attempt_id and stage_key:
            start_repair = int(
                self._generation_stage_state(generation_attempt_id, stage_key).get(
                    "repair_number"
                )
                or 0
            )
        for repair in range(start_repair, self.settings.contract_repair_attempts + 1):
            if generation_attempt_id and stage_key:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).begin_generation_stage(
                        generation_attempt_id,
                        stage_key,
                        repair_number=repair,
                        provider_thread_id=thread,
                    )
            turn = await self.backend.run_role(
                role=role,
                scope_id=scope_id,
                task=current,
                profile=profile,
                persistent=persistent,
                provider_thread_id=thread,
                repair_number=repair,
                on_thread_ready=(
                    self._generation_thread_callback(generation_attempt_id, stage_key)
                    if generation_attempt_id and stage_key
                    else None
                ),
                runtime_root=(
                    self._generation_runtime_root(generation_attempt_id)
                    if generation_attempt_id else None
                ),
                output_language=output_language,
            )
            if generation_attempt_id:
                self._record_generation_turn(generation_attempt_id, role, turn)
            thread = turn.thread_id or thread
            if thread and generation_attempt_id and stage_key:
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).bind_generation_stage_thread(
                        generation_attempt_id, stage_key, thread
                    )
            if turn.status != "completed":
                raise ContractGenerationInterrupted(
                    f"{role} transport 调用失败：{turn.error or turn.status}"
                )
            errors = self._validate_audit(turn.text)
            if not errors:
                return turn
            current = (
                "本轮审计报告未通过格式校验。请一次性修复全部问题：\n"
                + "\n".join(f"- {error}" for error in errors or [turn.error or turn.status])
                + "\n请重新提交完整审计报告。"
            )
        raise ContractWorkflowError(f"{role} 审计格式修复失败")

    def _generation_stage_state(
        self, generation_attempt_id: str, stage_key: str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            item = RepositoryHub(session, self.artifacts).get_generation_stage(
                generation_attempt_id, stage_key
            )
            return {
                "stage_key": item.stage_key,
                "status": item.status,
                "provider_thread_id": item.provider_thread_id,
                "artifact_path": item.artifact_path,
                "artifact_sha256": item.artifact_sha256,
                "repair_number": item.repair_number,
            }

    def _completed_stage_text(self, stage: dict[str, Any]) -> str | None:
        if stage.get("status") != "completed":
            return None
        reference = str(stage.get("artifact_path") or "")
        expected = str(stage.get("artifact_sha256") or "")
        if not reference or not expected:
            raise ContractWorkflowError(
                f"Completed stage `{stage.get('stage_key')}` 缺少冻结产物"
            )
        path = self.artifacts.resolve(reference)
        if not path.is_file() or sha256_file(path) != expected:
            raise ContractWorkflowError(
                f"Completed stage `{stage.get('stage_key')}` 产物缺失或 SHA 不匹配"
            )
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _resume_stage_task(task: str, stage: dict[str, Any]) -> str:
        if stage.get("status") != "interrupted":
            return task
        return (
            "上一次模型 turn 因服务器、Runner 或 transport 中断而没有形成已验收交付。"
            "现在继续同一阶段；请复用本 thread 已有上下文并重新提交该阶段的完整交付。\n\n"
            + task
        )

    def _generation_thread_callback(
        self, generation_attempt_id: str, stage_key: str
    ) -> Callable[[str], Awaitable[None]]:
        async def persist(provider_thread_id: str) -> None:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).bind_generation_stage_thread(
                    generation_attempt_id, stage_key, provider_thread_id
                )

        return persist

    def _persist_generation_stage(
        self,
        *,
        user_id: str,
        conversation_id: str,
        generation_attempt_id: str,
        stage_key: str,
        role: str,
        text: str,
        turn: AgentTurn,
    ) -> Path:
        stage = self._generation_stage_state(generation_attempt_id, stage_key)
        path = self._generation_stage_artifact_path(
            user_id, conversation_id,
            generation_attempt_id,
            stage_key,
            int(stage.get("repair_number") or 0),
        )
        self.artifacts.write_text(path.relative_to(self.artifacts.data_root), text)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.complete_generation_stage(
                generation_attempt_id,
                stage_key,
                provider_thread_id=turn.thread_id,
                artifact_path=path,
                artifact_sha256=sha256_file(path),
                repair_number=int(stage.get("repair_number") or 0),
                model=turn.model,
                reasoning_effort=turn.reasoning_effort,
            )
        return path

    def _interrupt_generation_attempt(
        self, generation_attempt_id: str, error: str
    ) -> None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.interrupt_running_generation_stages(generation_attempt_id, error)
            repo.set_generation_attempt_status(
                generation_attempt_id,
                "interrupted",
                error=error,
                event_type="contract_generation_interrupted",
                event_message="Contract generation 因基础设施中断而暂停；可从原 checkpoint 继续",
            )

    async def _validated_chat_turn(
        self,
        contract_version_id: str,
        *,
        user_id: str,
        conversation_id: str,
        chat_call_id: str,
        task: str,
        profile: str,
        provider_thread_id: str | None,
        on_stream_event: ChatStreamEvent | None = None,
        output_language: str = "zh",
    ) -> tuple[str, dict[str, str], AgentTurn]:
        current = task
        thread = provider_thread_id
        last: AgentTurn | None = None
        for repair in range(self.settings.contract_repair_attempts + 1):
            if repair and on_stream_event is not None:
                await on_stream_event("reset", {"content": "", "repair_number": repair})
            projector = ReplyStreamProjector(on_stream_event)
            turn = await self.backend.run_role(
                role="contract_chat_assistant",
                scope_id=contract_version_id,
                task=current,
                profile=profile,
                persistent=True,
                provider_thread_id=thread,
                repair_number=repair,
                on_text_delta=projector.feed,
                runtime_root=self.artifacts.conversation_runtime_root(
                    self._storage_key(user_id), conversation_id
                ) / "chat" / contract_version_id,
                output_language=output_language,
            )
            self._record_chat_turn(user_id, conversation_id, chat_call_id, turn)
            last = turn
            thread = turn.thread_id or thread
            try:
                reply, patches = self._parse_chat_response(turn.text)
            except ValueError as exc:
                current = (
                    "上一轮 Contract Chat 输出无法解析。请一次性修复以下问题：\n"
                    f"- {exc}\n"
                    "请重新提交 `# Contract Chat Response`，包含 `## Reply` 和 `## Draft Patches`；"
                    "Draft Patches 使用 JSON object 或 NONE。"
                )
                continue
            await projector.finish(reply)
            return reply, patches, turn
        raise ContractWorkflowError(last.error if last and last.error else "Contract Chat 输出格式修复失败")

    def _save_generated_contract(
        self,
        user_id: str,
        conversation_id: str,
        generation_attempt_id: str,
        document: ContractDocument,
        manifest: list[dict[str, Any]],
        *,
        execution_policy: dict[str, Any],
        created_by: str,
        warnings: list[str],
    ) -> GeneratedContract:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            conversation = repo.get_conversation(user_id, conversation_id)
            effective_policy = dict(conversation.execution_policy or {}) or dict(execution_policy)
            if not conversation.execution_policy:
                repo.update_conversation_execution_policy(
                    user_id, conversation_id, effective_policy
                )
            version_number = conversation.latest_contract_version + 1
            path = self._contract_path(user_id, conversation_id, version_number)
            self.artifacts.write_text(path.relative_to(self.artifacts.data_root), document.markdown)
            version = repo.create_contract_version(
                conversation,
                title=document.title,
                markdown_path=path,
                sections=document.sections,
                source_manifest=manifest,
                execution_policy=effective_policy,
                created_by=created_by,
            )
            repo.set_generation_attempt_status(
                generation_attempt_id,
                "completed",
                resulting_contract_version_id=version.contract_version_id,
                event_type="contract_version_created",
                event_message=f"Contract v{version.version} 已生成",
                event_payload={"version": version.version},
            )
            return GeneratedContract(
                conversation_id=conversation_id,
                version=version.version,
                contract_version_id=version.contract_version_id,
                markdown=document.markdown,
                warnings=warnings,
                execution_policy=dict(effective_policy),
            )

    # ------------------------------------------------------------------
    # Context / parsing helpers
    # ------------------------------------------------------------------
    def _default_execution_policy(self, user_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            user = RepositoryHub(session, self.artifacts).get_user(user_id)
            model = user.default_model_strategy
            duration = user.default_budget_preset
        return dict(self.settings.make_execution_policy(model, duration))

    def _source_context(self, manifest: list[dict[str, Any]], generation_attempt_id: str) -> str:
        if not manifest:
            return "当前没有用户上传的 Source Materials。"
        sections: list[str] = []
        material_dir = self._generation_runtime_root(generation_attempt_id) / "sources"
        material_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(manifest, start=1):
            name = str(item.get("original_name") or f"source-{index}")
            if str(item.get("availability") or "missing") != "available":
                sections.append(f"## Source {index}: {name}\n\n[missing/deleted]")
                continue
            raw_path = str(item.get("artifact_path") or "")
            path = self.artifacts.resolve(raw_path) if raw_path else Path()
            if not path.is_file():
                sections.append(f"## Source {index}: {name}\n\n[missing/deleted]")
                continue
            target = material_dir / f"{index:03d}-{path.name}"
            if not target.exists():
                shutil.copy2(path, target)
            text = self._extract_source_text(path)
            sections.append(
                f"## Source {index}: {name}\n\n"
                f"- file_id: {item.get('file_id')}\n"
                f"- sha256: {item.get('sha256')}\n"
                f"- local_path: {target}\n\n"
                f"{text[:24000]}"
            )
        return "\n\n".join(sections)

    @staticmethod
    def _extract_source_text(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt", ".tex"}:
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".pdf":
            try:
                pages = []
                for page in PdfReader(str(path)).pages:
                    pages.append(page.extract_text() or "")
                text = "\n\n".join(pages).strip()
                return text or "[该 PDF 没有稳定可提取的文本层；多模态理解仅为 best-effort。]"
            except Exception as exc:
                return f"[PDF 文本提取失败：{type(exc).__name__}；视觉内容仅为 best-effort。]"
        return "[unsupported source]"

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        cleaned = text.strip()
        fence = re.fullmatch(r"```(?:markdown)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            cleaned = fence.group(1).strip()
        start = cleaned.find("# Research Contract")
        if start >= 0:
            cleaned = cleaned[start:]
        return cleaned.rstrip() + "\n"

    @staticmethod
    def _validate_audit(markdown: str) -> list[str]:
        errors: list[str] = []
        if not markdown.strip():
            errors.append("审计报告为空")
        if "## Verdict" not in markdown:
            errors.append("缺少 `## Verdict`")
        if not re.search(r"\b(PASS|REVISE|REJECT)\b", markdown, flags=re.IGNORECASE):
            errors.append("Verdict 必须明确为 PASS / REVISE / REJECT")
        return errors

    @staticmethod
    def _parse_chat_response(text: str) -> tuple[str, dict[str, str]]:
        reply_match = re.search(r"^##\s+Reply\s*$\n(.*?)(?=^##\s+Draft Patches\s*$)", text, flags=re.MULTILINE | re.DOTALL)
        patch_match = re.search(r"^##\s+Draft Patches\s*$\n(.*)$", text, flags=re.MULTILINE | re.DOTALL)
        if not reply_match or not patch_match:
            raise ValueError("缺少 Reply 或 Draft Patches")
        reply = reply_match.group(1).strip()
        raw = patch_match.group(1).strip()
        if not reply:
            raise ValueError("Reply 不能为空")
        if raw.upper() in {"NONE", "NO PATCH", "NO_PATCH"}:
            return reply, {}
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        payload_text = fenced.group(1) if fenced else raw
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Draft Patches 不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Draft Patches 必须是 JSON object")
        patches: dict[str, str] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                raise ValueError(f"Patch `{key}` 必须是 Markdown string")
            patches[str(key)] = value
        return reply, patches

    @staticmethod
    def _ensure_editable(conversation: ContractConversationRecord) -> None:
        if conversation.status == "contract_generating":
            raise ContractWorkflowError(
                "该 Contract Conversation 已有生成任务正在运行；完成前不能并行修改 Contract"
            )
        if conversation.status == "generation_interrupted":
            raise ContractWorkflowError(
                "该 Contract Conversation 有中断的生成任务；请先继续 Contract 执行"
            )
        if conversation.locked_at or conversation.status in {"execution_locked", "running", "completed"}:
            raise ContractWorkflowError("该 Contract Conversation 已进入执行阶段，不能继续修改")

    @staticmethod
    def _request_draft_payload(item: ContractConversationRecord) -> dict[str, Any]:
        return {
            "conversation_id": item.conversation_id,
            "project_id": item.project_id,
            "display_name": item.display_name,
            "research_problem": item.research_problem,
            "background_and_prior_progress": item.background_and_prior_progress,
            "method_preferences_and_restrictions": item.method_preferences_and_restrictions,
            "valuable_partial_outcomes": item.valuable_partial_outcomes,
            "possible_ideas_and_rough_approaches": item.possible_ideas_and_rough_approaches,
            "contractor_profile": item.contractor_profile,
            "contract_language": normalize_language(item.contract_language),
            "source_file_ids": list(item.source_file_ids),
            "draft_revision": item.request_draft_revision,
            "status": item.status,
        }

    def _storage_key(self, user_id: str) -> str:
        with self.database.session() as session:
            user = RepositoryHub(session, self.artifacts).get_user_any_status(user_id)
            if not user.storage_key:
                raise ContractWorkflowError("当前用户缺少 storage_key")
            return user.storage_key

    def _generation_runtime_root(self, generation_attempt_id: str) -> Path:
        with self.database.session() as session:
            attempt = session.get(ContractGenerationAttemptRecord, generation_attempt_id)
            if attempt is None:
                raise ContractWorkflowError("Contract generation attempt 不存在")
            user = RepositoryHub(session, self.artifacts).get_user_any_status(attempt.owner_user_id)
            if not user.storage_key:
                raise ContractWorkflowError("当前用户缺少 storage_key")
            return (
                self.artifacts.conversation_runtime_root(user.storage_key, attempt.conversation_id)
                / "attempts" / generation_attempt_id
            )

    def _record_generation_turn(self, generation_attempt_id: str, role: str, turn: AgentTurn) -> None:
        """每个 generation turn 立即入 Usage；repair 与失败 turn 也不能漏计。"""
        with self.database.session() as session:
            attempt = session.get(ContractGenerationAttemptRecord, generation_attempt_id)
            if attempt is None:
                raise ContractWorkflowError("Contract generation attempt 不存在")
            RepositoryHub(session, self.artifacts).record_usage(
                user_id=attempt.owner_user_id,
                conversation_id=attempt.conversation_id,
                attempt_id=generation_attempt_id,
                component="planner",
                role=role,
                model=turn.model,
                usage=turn.usage,
                provider_thread_id=turn.thread_id,
                usage_semantics=turn.usage_semantics,
                cost_estimator=lambda normalized: self.settings.estimate_turn_cost(
                    turn.model, normalized
                ),
            )

    def _record_chat_turn(self, user_id: str, conversation_id: str, chat_call_id: str, turn: AgentTurn) -> None:
        """每个 Contract Chat turn 立即入账，格式修复同样收费。"""
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).record_usage(
                user_id=user_id,
                conversation_id=conversation_id,
                attempt_id=chat_call_id,
                component="planner",
                role="contract_chat_assistant",
                model=turn.model,
                usage=turn.usage,
                provider_thread_id=turn.thread_id,
                usage_semantics=turn.usage_semantics,
                cost_estimator=lambda normalized: self.settings.estimate_turn_cost(
                    turn.model, normalized
                ),
            )

    def _contract_profile_models(self, profile: str) -> list[str]:
        """列出一次 Contract pipeline 可能调用的模型，供 Credit pricing admission。"""
        table = self.backend.config.model_profiles.get(profile) if hasattr(self.backend, "config") else None
        if not table:
            return []
        return sorted({item.model for item in table.values()})

    def _generation_snapshot_path(self, user_id: str, conversation_id: str, kind: str) -> Path:
        return self.artifacts.conversation_runtime_root(
            self._storage_key(user_id), conversation_id
        ) / "generation_inputs" / f"{utc_now().strftime('%Y%m%dT%H%M%S%f')}-{kind}.json"

    def _contractor_diagnosis_path(self, user_id: str, conversation_id: str, attempt_id: str) -> Path:
        """返回本次生成内部 Stage 1 数学诊断的稳定 artifact 路径。"""
        return (
            self.artifacts.conversation_runtime_root(self._storage_key(user_id), conversation_id)
            / "generation_attempts"
            / attempt_id
            / "contractor-stage1-diagnosis.md"
        )

    def _generation_stage_artifact_path(
        self,
        user_id: str,
        conversation_id: str,
        attempt_id: str,
        stage_key: str,
        repair_number: int,
    ) -> Path:
        """为每次通过验收的阶段输出分配不可覆盖的 artifact 路径。"""
        return (
            self.artifacts.conversation_runtime_root(self._storage_key(user_id), conversation_id)
            / "generation_attempts"
            / attempt_id
            / "stages"
            / stage_key
            / f"repair-{repair_number:02d}-{new_id('artifact')}.md"
        )

    def _contract_path(self, user_id: str, conversation_id: str, version: int) -> Path:
        return self.artifacts.conversation_root(
            self._storage_key(user_id), conversation_id
        ) / f"contract-v{version:03d}.md"

    @staticmethod
    def _draft_payload(draft: Any, validation: ContractValidation | None = None) -> dict[str, Any]:
        if draft is None:
            return {}
        return {
            "base_version": draft.base_version,
            "title": draft.title,
            "sections": dict(draft.sections),
            "source_manifest": list(draft.source_manifest),
            "contract_language": normalize_language(
                getattr(draft, "contract_language", "zh")
            ),
            "draft_revision": int(draft.draft_revision),
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "validation": {
                "errors": validation.errors if validation else [],
                "warnings": validation.warnings if validation else [],
            },
        }

    @staticmethod
    def _contractor_stage1_task(snapshot: dict[str, Any], sources: str) -> str:
        kind = str(snapshot.get("kind") or "initial")
        if kind == "full_regeneration":
            return f"""现在执行 Contractor Stage 1。只形成内部 `Effective Problem Brief and Mathematical Diagnosis`，不要生成 `# Research Contract`。

你必须综合下面全部输入，先判断哪些内容构成当前有效用户意图。`本次补充说明` 默认是明确修改意图；Contract Chat 中普通提问和试探性猜测不能自动升级为硬约束。当前 immutable Contract 是数学基准而不是改写模板；最终新版本必须像第一次写出一样 standalone。

# Original Research Request

{snapshot.get('research_request', '')}

# Current Immutable Contract

{snapshot.get('base_contract_markdown', '')}

# Current Working Draft

{json.dumps(snapshot.get('working_draft'), ensure_ascii=False, indent=2)}

# Version-scoped Contract Chat

{json.dumps(snapshot.get('contract_chat'), ensure_ascii=False, indent=2)}

# 本次补充说明

{snapshot.get('user_revision_instruction') or '未提供。'}

# Source Materials

{sources}

本阶段需要：归并 Effective Problem、识别 superseded framing、正规化对象与量词、压缩 3–5 个核心未知、做必要的实际数学探索、比较不同机制、寻找 minimal sufficient bridges，并形成 D/E/F 的数学骨架。不要输出最终 Contract，不要写版本修改日志。
"""
        return f"""现在执行 Contractor Stage 1。只形成内部 `Effective Problem Brief and Mathematical Diagnosis`，不要生成 `# Research Contract`。

请先真正理解问题，再设计研究计划。Possible Ideas 只是未经验证线索；Source Materials 只在其内容可读且确实支持时使用。

# Original Research Request

{snapshot.get('research_request', '')}

# Source Materials

{sources}

本阶段需要：正规化当前数学问题、对象和量词；压缩 3–5 个核心未知；做必要的实际数学探索；提出少量 genuinely different candidate mechanisms；识别 minimal sufficient bridges；形成 2–5 个 Detailed Nodes 与后续方向的数学骨架。不要输出最终 Contract。
"""

    @staticmethod
    def _contractor_stage2_task() -> str:
        return """现在执行 Contractor Stage 2。

基于你刚完成的 Effective Problem Brief 与 Mathematical Diagnosis，从头撰写一份独立、standalone、user-facing 的完整 Research Contract。

必须像第一次自然写出的当前数学研究计划：不要提上一版、修订过程、Chat、Stage 1、Auditor 或 Controller。内部思考可以很深，但用户可见概念应少而具体。Unknown Slots 通常 3–5 个并充分展开；Outcome Tree 通常 2–4 个主分支；Structural Diagnosis 可以是最长部分之一；Detailed Nodes 通常 2–5 个，严格使用“和原问题的关系 / 当前目标 / 当前输入 / 首轮方式”；Structured Directions 必须 self-contained；F 通常 3–4 条数学 horizon。

只输出固定 A–F `# Research Contract` Markdown，不输出 G、解释前言、版本说明或 diff。
"""

    @staticmethod
    def _generation_context(snapshot: dict[str, Any]) -> str:
        if str(snapshot.get("kind") or "initial") != "full_regeneration":
            return f"""# Original Research Request

{snapshot.get('research_request', '')}
"""
        return f"""# Original Research Request

{snapshot.get('research_request', '')}

# Current Immutable Contract

{snapshot.get('base_contract_markdown', '')}

# Current Working Draft

{json.dumps(snapshot.get('working_draft'), ensure_ascii=False, indent=2)}

# Version-scoped Contract Chat

{json.dumps(snapshot.get('contract_chat'), ensure_ascii=False, indent=2)}

# 本次补充说明

{snapshot.get('user_revision_instruction') or '未提供。'}
"""

    @classmethod
    def _audit_task(
        cls,
        snapshot: dict[str, Any],
        sources: str,
        diagnosis: str,
        candidate: str,
    ) -> str:
        return f"""请独立审查下面的 Research Contract 候选。

你必须根据当前有效用户输入判断 fidelity、version freshness、概念完整性/过度碎片化，以及 D/E/F 是否真正数学化、self-contained、适合数学家阅读。Stage 1 Diagnosis 只是 Contractor 的内部分析，不可凌驾于用户输入。

{cls._generation_context(snapshot)}

# Source Materials

{sources}

# Contractor Stage 1 Mathematical Diagnosis

{diagnosis}

# Candidate Contract

{candidate}
"""

    @classmethod
    def _integration_task(
        cls,
        snapshot: dict[str, Any],
        sources: str,
        diagnosis: str,
        candidate: str,
        audits: str,
    ) -> str:
        return f"""请作为 fresh Contract Integrator，根据当前有效用户输入、Stage 1 数学诊断和三份审计报告修订候选。

你必须修复真实问题、合并过度碎片化条目，并把 Auditor 的工程/审计语言翻译成自然数学语言。最终 Contract 必须 standalone；不能暴露旧版本或修订过程。只输出固定 A–F，不输出 G。

{cls._generation_context(snapshot)}

# Source Materials

{sources}

# Contractor Stage 1 Mathematical Diagnosis

{diagnosis}

# Candidate Contract

{candidate}

# Audits

{audits}
"""

    @staticmethod
    def _contract_chat_task(
        conversation: ContractConversationRecord,
        draft: Any,
        history: list[dict[str, str]],
        latest_message: str,
        *,
        changes_allowed: bool,
    ) -> str:
        return f"""你是当前 Research Contract 的精简讨论与 Working Draft 编辑助手。

原始 Research Request 只作为隐藏背景，不要在可见回复中逐字复述。普通提问只回答；只有用户明确要求修改且 Controller 明确允许本轮修改时，才通过 Draft Patches 修改 A–F。G Execution Policy 只能由用户在结构化表单中修改。

# 本轮 Draft 修改权限

{"ALLOWED" if changes_allowed else "DISABLED：本轮 Draft Patches 必须为 NONE"}

# Hidden Original Research Request

{conversation.research_request}

# Current Working Draft

{render_contract(draft.title, dict(draft.sections))}

# Visible Chat History

{json.dumps(history, ensure_ascii=False, indent=2)}

# Latest User Message

{latest_message}

请严格输出：

# Contract Chat Response

## Reply

[简洁回答]

## Draft Patches

NONE

若需要修改，Draft Patches 改为 JSON object，key 必须是允许的 A–F section key，value 是该 Section 的完整 Markdown 正文。
"""
