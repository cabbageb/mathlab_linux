from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from math_planner.config import load_config as load_planner_config
from math_prover.config import load_config as load_prover_config
from math_prover.vnext_controller import DynamicResearchController

from .agent_backend import (
    ContractCodexBackend,
    OfflineContractBackend,
    OfflineProverBackend,
    ProverCodexBackend,
    prover_model_profiles,
)
from .artifacts import ArtifactStore
from .contract_service import ContractService
from .deletion_service import DeletionExecutor
from .db import Database
from .repositories import RepositoryHub
from .runtime_gc import RuntimeGcService
from .settings import VNextSettings


class ResearchRunnerService:
    """Database-backed owner of long Contract generation and Research Run jobs."""

    def __init__(self, settings: VNextSettings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url, echo=settings.database_echo)
        self.artifacts = ArtifactStore(settings.data_root)
        planner_config = load_planner_config(settings.planner_config)
        prover_config = load_prover_config(settings.prover_config)
        if settings.offline_agents:
            self.contract_backend: Any = OfflineContractBackend()
            self.prover_backend: Any = OfflineProverBackend()
        else:
            self.contract_backend = ContractCodexBackend(
                planner_config,
                settings.root / "planner/prompts/contract",
                self.artifacts.platform_root / "services" / "contractor",
            )
            self.prover_backend = ProverCodexBackend(
                prover_config,
                settings.root / "prover/prompts/vnext",
                self.artifacts.platform_root / "services" / "prover",
                compute_timeout_seconds=settings.compute_tool_timeout_seconds,
            )
        self.contracts = ContractService(
            settings,
            self.database,
            self.artifacts,
            self.contract_backend,
            prover_model_profiles(prover_config),
        )
        self.controller = DynamicResearchController(
            settings, self.database, self.artifacts, self.prover_backend
        )
        self._stop = asyncio.Event()
        self._active_runs: dict[str, asyncio.Task[None]] = {}
        self._active_generations: dict[str, asyncio.Task[None]] = {}
        self._active_deletions: dict[str, asyncio.Task[None]] = {}
        self.deletions = DeletionExecutor(self.database, self.artifacts, self.controller, self._active_generations)
        self.runtime_gc = RuntimeGcService(
            self.database, self.artifacts, self.settings.runner_id,
            self.settings.runtime_retention_days,
        )
        self._last_runtime_gc = 0.0

    async def start(self) -> None:
        self.settings.data_root.mkdir(parents=True, exist_ok=True)
        if self.settings.auto_create_schema:
            self.database.initialize()
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.ensure_identity_policies()
            repo.ensure_bootstrap_user(
                self.settings.bootstrap_admin_email,
                self.settings.bootstrap_admin_credit,
                self.settings.developer_max_concurrent_runs,
            )
            repo.sync_role_run_limits(
                admin_limit=self.settings.developer_max_concurrent_runs,
                user_limit=self.settings.user_max_concurrent_runs,
            )
            repo.release_stale_run_claims()
            repo.release_stale_generation_claims()
            repo.release_stale_deletion_claims()
            repo.heartbeat_runner(self.settings.runner_id, status="starting")
        await self.contract_backend.start()
        await self.controller.start()

    async def stop(self) -> None:
        self._stop.set()
        await self.controller.stop()
        if self._active_generations:
            tasks = list(self._active_generations.values())
            _, pending = await asyncio.wait(
                tasks, timeout=max(0.1, float(self.settings.graceful_shutdown_soft_seconds))
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._active_runs:
            await asyncio.gather(*self._active_runs.values(), return_exceptions=True)
        if self._active_deletions:
            await asyncio.gather(*self._active_deletions.values(), return_exceptions=True)
        await self.contract_backend.stop()
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).heartbeat_runner(
                self.settings.runner_id, status="stopped"
            )
        self.database.dispose()

    async def serve(self) -> None:
        await self.start()
        try:
            while not self._stop.is_set():
                self._reap_finished()
                self._claim_deletion_jobs()
                if time.monotonic() - self._last_runtime_gc >= self.settings.runtime_gc_poll_seconds:
                    await asyncio.to_thread(self.runtime_gc.sweep_once)
                    self._last_runtime_gc = time.monotonic()
                if not self._active_deletions:
                    self._claim_generation_jobs()
                    self._claim_research_runs()
                current = next(iter(self._active_runs), None)
                with self.database.session() as session:
                    RepositoryHub(session, self.artifacts).heartbeat_runner(
                        self.settings.runner_id,
                        status="busy" if (self._active_runs or self._active_generations) else "idle",
                        current_run_id=current,
                        metadata={
                            "active_run_ids": sorted(self._active_runs),
                            "active_generation_attempt_ids": sorted(self._active_generations),
                            "active_deletion_job_ids": sorted(self._active_deletions),
                        },
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.runner_poll_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop.set()

    def _claim_generation_jobs(self) -> None:
        while True:
            with self.database.session() as session:
                attempt = RepositoryHub(session, self.artifacts).claim_next_generation_attempt(
                    self.settings.runner_id,
                    admin_limit=self.settings.admin_contract_generation_max_concurrent,
                    user_limit=self.settings.user_contract_generation_max_concurrent,
                )
                if attempt is None:
                    return
                attempt_id = attempt.generation_attempt_id
            task = asyncio.create_task(
                self.contracts.execute_generation_attempt(attempt_id),
                name=f"runner-contract-generation-{attempt_id}",
            )
            self._active_generations[attempt_id] = task

    def _claim_deletion_jobs(self) -> None:
        if self._active_deletions:
            return
        with self.database.session() as session:
            item = RepositoryHub(session, self.artifacts).claim_next_deletion_job(self.settings.runner_id)
            if item is None:
                return
            job_id = item.deletion_job_id
        self._active_deletions[job_id] = asyncio.create_task(
            self.deletions.execute(job_id, timeout=float(self.settings.graceful_shutdown_hard_seconds)),
            name=f"runner-delete-{job_id}",
        )

    def _claim_research_runs(self) -> None:
        while True:
            claimed = self._claim_one_run()
            if claimed is None:
                return
            run_id, user_id, attempt_id = claimed
            self.controller.launch_run(user_id, run_id, attempt_id)
            task = asyncio.create_task(
                self.controller.wait_run(run_id), name=f"runner-wait-{run_id}"
            )
            self._active_runs[run_id] = task

    def _claim_one_run(self) -> tuple[str, str, str] | None:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = repo.claim_next_queued_run(
                self.settings.runner_id,
                admin_limit=self.settings.admin_runner_max_concurrent_runs,
                user_limit=self.settings.user_runner_max_concurrent_runs,
            )
            if run is None or not run.attempts:
                return None
            return run.run_id, run.owner_user_id, run.attempts[-1].attempt_id

    def _reap_finished(self) -> None:
        for mapping in (self._active_runs, self._active_generations, self._active_deletions):
            for key, task in list(mapping.items()):
                if not task.done():
                    continue
                with suppress(asyncio.CancelledError, Exception):
                    task.result()
                mapping.pop(key, None)
