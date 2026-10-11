from __future__ import annotations

import asyncio

from sqlalchemy import select

from .artifacts import ArtifactStore
from .db import ContractGenerationAttemptRecord, Database, DeletionJobRecord, ProjectRecord, ResearchRunRecord
from .repositories import RepositoryHub


class DeletionExecutor:
    """执行持久化终止删除任务：先停生成/Run，再删除目标，账本保持独立。"""

    def __init__(self, database: Database, artifacts: ArtifactStore, controller, generation_tasks: dict[str, asyncio.Task]) -> None:
        self.database = database
        self.artifacts = artifacts
        self.controller = controller
        self.generation_tasks = generation_tasks

    async def execute(self, deletion_job_id: str, *, timeout: float) -> None:
        try:
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                job = session.get(DeletionJobRecord, deletion_job_id)
                if job is None:
                    return
                owner_user_id = job.owner_user_id
                target_type = job.target_type
                target_id = job.target_id
                conversation_ids = repo.deletion_conversation_ids(job)
                generation_ids = list(session.scalars(select(ContractGenerationAttemptRecord.generation_attempt_id).where(
                    ContractGenerationAttemptRecord.conversation_id.in_(conversation_ids),
                    ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing"]),
                ))) if conversation_ids else []
                runs = list(session.execute(select(ResearchRunRecord.run_id, ResearchRunRecord.owner_user_id).where(
                    ResearchRunRecord.conversation_id.in_(conversation_ids),
                    ResearchRunRecord.status.in_(["created", "queued", "resuming", "running", "draining", "paused"]),
                ))) if conversation_ids else []

            active_generation_tasks = [self.generation_tasks[item] for item in generation_ids if item in self.generation_tasks and not self.generation_tasks[item].done()]
            for task in active_generation_tasks:
                task.cancel()
            if active_generation_tasks:
                _, pending = await asyncio.wait(active_generation_tasks, timeout=max(0.1, timeout))
                if pending:
                    raise RuntimeError("Contract generation 未能在删除截止时间内停止；目标未删除，可重试")
            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                for generation_id in generation_ids:
                    attempt = session.get(ContractGenerationAttemptRecord, generation_id)
                    if attempt is not None and attempt.status in {"queued", "running", "repairing"}:
                        repo.set_generation_attempt_status(generation_id, "failed", error="目标正在永久删除")

            for run_id, run_owner in runs:
                try:
                    await asyncio.wait_for(
                        self.controller.terminate_run_for_deletion(
                            run_owner, run_id, timeout=timeout
                        ),
                        timeout=max(0.1, timeout),
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "Research Run 终止超过删除总截止时间；目标未删除，可重试"
                    ) from exc

            with self.database.session() as session:
                repo = RepositoryHub(session, self.artifacts)
                if target_type == "project":
                    target = session.get(ProjectRecord, target_id)
                    if target is not None and target.owner_user_id == owner_user_id:
                        repo.delete_project(owner_user_id, target_id)
                else:
                    conversation_ids = repo._deletion_conversation_ids(owner_user_id, "conversation", target_id)
                    if conversation_ids:
                        repo.delete_conversation(owner_user_id, target_id)
                repo.finish_deletion_job(deletion_job_id)
        except Exception as exc:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).finish_deletion_job(deletion_job_id, error=str(exc))
