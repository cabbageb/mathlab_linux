from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil
import json

from sqlalchemy import delete, or_, select

from .artifacts import ArtifactStore
from .db import (
    AgentThreadRecord,
    ArchitectureCommunicationRecord,
    ArchitectureDecisionLedgerRecord,
    ArchitectureReviewRecord,
    ArchitectureSnapshotRecord,
    ContractGenerationAttemptRecord,
    ContractGenerationStageRecord,
    Database,
    PromotionProposalRecord,
    GlobalStateDeltaRecord,
    ResearchFindingRecord,
    ResearchNodeRecord,
    ResearchRunRecord,
    RuntimeEventRecord,
    UserRecord,
    VerifiedClaimRecord,
    WorkerIdentityRecord,
    utc_now,
)


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
TERMINAL_GENERATION_STATUSES = {"completed", "failed"}


class RuntimeGcService:
    """幂等删除已过期内部过程；公开数学投影、论文和账本始终保留。"""

    def __init__(self, database: Database, artifacts: ArtifactStore, runner_id: str, retention_days: int = 7) -> None:
        self.database = database
        self.artifacts = artifacts
        self.runner_id = runner_id
        self.retention_days = max(0, int(retention_days))

    def sweep_once(self) -> dict[str, object] | None:
        quarantine = self._clean_expired_legacy_quarantine()
        if quarantine is not None:
            return quarantine
        claimed = self._claim_due()
        if claimed is None:
            return None
        kind, item_id = claimed
        try:
            if kind == "run":
                freed = self._clean_run(item_id)
            else:
                freed = self._clean_generation(item_id)
            return {"kind": kind, "id": item_id, "status": "completed", "freed_bytes": freed}
        except Exception as exc:
            self._record_failure(kind, item_id, str(exc))
            return {"kind": kind, "id": item_id, "status": "failed", "error": str(exc)}

    def _claim_due(self) -> tuple[str, str] | None:
        now = utc_now()
        with self.database.session() as session:
            run_statement = select(ResearchRunRecord).where(
                ResearchRunRecord.status.in_(TERMINAL_RUN_STATUSES),
                ResearchRunRecord.runtime_expires_at.is_not(None),
                ResearchRunRecord.runtime_expires_at <= now,
                ResearchRunRecord.runtime_cleaned_at.is_(None),
                ResearchRunRecord.keep_runtime.is_(False),
                ResearchRunRecord.final_snapshot_completed_at.is_not(None),
                ResearchRunRecord.runtime_cleanup_claimed_by.is_(None),
            ).order_by(ResearchRunRecord.runtime_expires_at).limit(1)
            if session.bind is not None and session.bind.dialect.name != "sqlite":
                run_statement = run_statement.with_for_update(skip_locked=True)
            run = session.scalar(run_statement)
            if run is not None:
                run.runtime_cleanup_claimed_by = self.runner_id
                run.runtime_cleanup_claimed_at = now
                run.runtime_cleanup_attempts = int(run.runtime_cleanup_attempts or 0) + 1
                run.runtime_cleanup_error = None
                return "run", run.run_id

            attempt_statement = select(ContractGenerationAttemptRecord).where(
                ContractGenerationAttemptRecord.status.in_(TERMINAL_GENERATION_STATUSES),
                ContractGenerationAttemptRecord.runtime_expires_at.is_not(None),
                ContractGenerationAttemptRecord.runtime_expires_at <= now,
                ContractGenerationAttemptRecord.runtime_cleaned_at.is_(None),
                ContractGenerationAttemptRecord.keep_runtime.is_(False),
                ContractGenerationAttemptRecord.runtime_cleanup_claimed_by.is_(None),
            ).order_by(ContractGenerationAttemptRecord.runtime_expires_at).limit(1)
            if session.bind is not None and session.bind.dialect.name != "sqlite":
                attempt_statement = attempt_statement.with_for_update(skip_locked=True)
            attempt = session.scalar(attempt_statement)
            if attempt is not None:
                attempt.runtime_cleanup_claimed_by = self.runner_id
                attempt.runtime_cleanup_claimed_at = now
                attempt.runtime_cleanup_attempts = int(attempt.runtime_cleanup_attempts or 0) + 1
                attempt.runtime_cleanup_error = None
                return "generation", attempt.generation_attempt_id
        return None

    def _clean_expired_legacy_quarantine(self) -> dict[str, object] | None:
        root = self.artifacts.platform_root / "legacy-runtime-quarantine"
        marker = root / ".retention.json"
        if not marker.is_file():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            expires = str(payload.get("expires_at") or "")
            from datetime import datetime, timezone
            deadline = datetime.fromisoformat(expires)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if deadline > utc_now():
            return None
        freed = self._remove_tree(root)
        return {"kind": "legacy_quarantine", "status": "completed", "freed_bytes": freed}

    def _clean_run(self, run_id: str) -> int:
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None or run.status not in TERMINAL_RUN_STATUSES:
                raise RuntimeError("Run 不存在或不再是 terminal")
            if run.keep_runtime or run.final_snapshot_completed_at is None:
                raise RuntimeError("Run runtime 当前受保留或 final snapshot 未完成")
            run_root = self.artifacts.resolve(run.run_root)
            if not run_root.is_relative_to(self.artifacts.users_root):
                raise RuntimeError("Run 路径不在 user-scoped 根")
            runtime_root = run_root / "runtime"
        freed = self._remove_tree(runtime_root)

        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return freed
            nodes = list(session.scalars(select(ResearchNodeRecord).where(ResearchNodeRecord.run_id == run_id)))
            node_ids = [item.node_id for item in nodes]
            worker_ids = list(session.scalars(
                select(WorkerIdentityRecord.worker_id).where(WorkerIdentityRecord.node_id.in_(node_ids))
            )) if node_ids else []
            finding_ids = list(session.scalars(
                select(ResearchFindingRecord.finding_id).where(ResearchFindingRecord.node_id.in_(node_ids))
            )) if node_ids else []
            if finding_ids:
                session.execute(delete(PromotionProposalRecord).where(PromotionProposalRecord.finding_id.in_(finding_ids)))
            if node_ids:
                session.execute(delete(ResearchFindingRecord).where(ResearchFindingRecord.node_id.in_(node_ids)))
                session.execute(delete(WorkerIdentityRecord).where(WorkerIdentityRecord.node_id.in_(node_ids)))
            session.execute(delete(ArchitectureReviewRecord).where(ArchitectureReviewRecord.run_id == run_id))
            session.execute(delete(ArchitectureCommunicationRecord).where(ArchitectureCommunicationRecord.run_id == run_id))
            session.execute(delete(GlobalStateDeltaRecord).where(GlobalStateDeltaRecord.run_id == run_id))
            session.execute(delete(ArchitectureDecisionLedgerRecord).where(ArchitectureDecisionLedgerRecord.run_id == run_id))
            session.execute(delete(ArchitectureSnapshotRecord).where(ArchitectureSnapshotRecord.run_id == run_id))
            scope_ids = [run_id, *node_ids, *worker_ids]
            if scope_ids:
                session.execute(delete(AgentThreadRecord).where(AgentThreadRecord.scope_id.in_(scope_ids)))
                session.execute(delete(RuntimeEventRecord).where(
                    RuntimeEventRecord.scope_id.in_(scope_ids),
                    RuntimeEventRecord.scope_type != "public_run",
                ))
            claim_ids = list(session.scalars(
                select(VerifiedClaimRecord.verified_claim_id).where(VerifiedClaimRecord.run_id == run_id)
            ))
            if claim_ids:
                session.execute(delete(RuntimeEventRecord).where(
                    RuntimeEventRecord.scope_type == "public_summary",
                    RuntimeEventRecord.scope_id.in_(claim_ids),
                ))
            for claim in session.scalars(select(VerifiedClaimRecord).where(VerifiedClaimRecord.run_id == run_id)):
                if claim.candidate_path:
                    candidate_path = self.artifacts.resolve(claim.candidate_path)
                    if candidate_path.is_relative_to(runtime_root):
                        claim.candidate_path = None
                claim.verifier_report_path = None
            for node in nodes:
                node.details = {}; node.final_stage_path = None; node.final_stage_sha256 = None
                node.origin_finding_id = None; node.origin_review_id = None
            run.runtime_cleaned_at = utc_now(); run.runtime_cleanup_claimed_by = None
            run.runtime_cleanup_claimed_at = None; run.runtime_cleanup_error = None
        return freed

    def _clean_generation(self, generation_attempt_id: str) -> int:
        with self.database.session() as session:
            attempt = session.get(ContractGenerationAttemptRecord, generation_attempt_id)
            if attempt is None or attempt.status not in TERMINAL_GENERATION_STATUSES:
                raise RuntimeError("Generation attempt 不存在或不可清理")
            if attempt.keep_runtime:
                raise RuntimeError("Generation runtime 已被 Developer 保留")
            user = session.get(UserRecord, attempt.owner_user_id)
            if user is None or not user.storage_key:
                raise RuntimeError("Generation owner 缺少 storage_key")
            conversation_runtime = self.artifacts.conversation_runtime_root(user.storage_key, attempt.conversation_id)
            roots = [
                conversation_runtime / "attempts" / generation_attempt_id,
                conversation_runtime / "generation_attempts" / generation_attempt_id,
            ]
            snapshot = self.artifacts.resolve(attempt.request_snapshot_path) if attempt.request_snapshot_path else None
        freed = sum(self._remove_tree(path) for path in roots)
        if snapshot is not None and snapshot.is_file() and snapshot.is_relative_to(conversation_runtime):
            freed += snapshot.stat().st_size; snapshot.unlink()

        with self.database.session() as session:
            attempt = session.get(ContractGenerationAttemptRecord, generation_attempt_id)
            if attempt is None:
                return freed
            for stage in session.scalars(select(ContractGenerationStageRecord).where(
                ContractGenerationStageRecord.generation_attempt_id == generation_attempt_id
            )):
                stage.artifact_path = None; stage.artifact_sha256 = None
                stage.provider_thread_id = None
            session.execute(delete(AgentThreadRecord).where(
                AgentThreadRecord.scope_id == generation_attempt_id
            ))
            session.execute(delete(RuntimeEventRecord).where(
                RuntimeEventRecord.scope_type == "contract_generation",
                RuntimeEventRecord.scope_id == generation_attempt_id,
            ))
            attempt.request_snapshot_path = None
            attempt.runtime_cleaned_at = utc_now(); attempt.runtime_cleanup_claimed_by = None
            attempt.runtime_cleanup_claimed_at = None; attempt.runtime_cleanup_error = None
        return freed

    def _record_failure(self, kind: str, item_id: str, error: str) -> None:
        with self.database.session() as session:
            item = session.get(
                ResearchRunRecord if kind == "run" else ContractGenerationAttemptRecord,
                item_id,
            )
            if item is not None:
                item.runtime_cleanup_claimed_by = None
                item.runtime_cleanup_claimed_at = None
                item.runtime_cleanup_error = error[:4000]

    @staticmethod
    def _remove_tree(path: Path) -> int:
        if not path.exists():
            return 0
        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())
        shutil.rmtree(path)
        return total

    def set_keep_runtime(self, run_id: str, keep: bool) -> ResearchRunRecord:
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise FileNotFoundError("Research Run 不存在")
            run.keep_runtime = bool(keep)
            if not keep and run.terminal_at is not None:
                run.runtime_expires_at = utc_now() + timedelta(days=self.retention_days)
            return run

    def dry_run(self, run_id: str) -> dict[str, object]:
        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise FileNotFoundError("Research Run 不存在")
            runtime = self.artifacts.resolve(run.run_root) / "runtime"
            files = [item for item in runtime.rglob("*") if item.is_file()] if runtime.is_dir() else []
            return {
                "run_id": run_id,
                "path": self.artifacts.reference(runtime),
                "files": len(files),
                "bytes": sum(item.stat().st_size for item in files),
                "eligible": bool(
                    run.status in TERMINAL_RUN_STATUSES
                    and run.final_snapshot_completed_at is not None
                    and not run.keep_runtime
                ),
            }
