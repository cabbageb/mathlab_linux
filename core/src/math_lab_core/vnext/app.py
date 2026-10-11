from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
import asyncio
import hmac
import json
import os
import re
import secrets
import time

from fastapi import Body, Cookie, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from math_planner.config import load_config as load_planner_config
from math_prover.config import load_config as load_prover_config
from math_prover.vnext_controller import DynamicProverError, DynamicResearchController

from .agent_backend import (
    ContractCodexBackend, ProverCodexBackend,
    OfflineContractBackend, OfflineProverBackend,
    prover_model_profiles,
)
from .artifacts import ArtifactStore
from .auth import AuthService
from .contract_schema import EDITABLE_KEYS, SECTION_SPECS, parse_contract, render_contract, validate_contract
from .contract_service import ContractService, ContractWorkflowError
from .db import Database, utc_iso, utc_now
from .deletion_service import DeletionExecutor
from .repositories import OwnershipError, RepositoryHub
from .runtime_gc import RuntimeGcService
from .settings import VNextSettings


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


def _usage_window_since(window: str):
    """把用户选择的滚动窗口转换为 Usage Ledger 的 UTC 下界。"""
    days = {"1d": 1, "7d": 7, "30d": 30}
    return None if window == "all" else utc_now() - timedelta(days=days[window])


class RenamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=240)


class ConversationCreate(BaseModel):
    """Backward-compatible one-shot request; new UI uses request-draft endpoints."""
    research_request: str = ""
    research_problem: str = ""
    background_and_prior_progress: str = ""
    method_preferences_and_restrictions: str = ""
    valuable_partial_outcomes: str = ""
    possible_ideas_and_rough_approaches: str = ""
    display_name: str | None = None
    project_id: str | None = None
    source_file_ids: list[str] = Field(default_factory=list)
    generate_contract: bool = False
    profile: str = "high"
    contract_language: Literal["zh", "en"] | None = None


class RequestDraftPayload(BaseModel):
    research_problem: str = Field(min_length=1)
    background_and_prior_progress: str = ""
    method_preferences_and_restrictions: str = ""
    valuable_partial_outcomes: str = ""
    possible_ideas_and_rough_approaches: str = ""
    contractor_profile: str = "high"
    contract_language: Literal["zh", "en"] | None = None
    project_id: str | None = None
    source_file_ids: list[str] = Field(default_factory=list)
    display_name: str | None = None
    expected_revision: int = Field(default=0, ge=0)


class ConversationMove(BaseModel):
    project_id: str | None = None


class NavigationReorderPayload(BaseModel):
    entity_type: str
    entity_id: str
    target_project_id: str | None = None
    target_index: int = Field(ge=0)


class ConversationSources(BaseModel):
    file_ids: list[str] = Field(default_factory=list)


class GeneratePayload(BaseModel):
    idempotency_key: str | None = None


class FullRegenerationPayload(BaseModel):
    selected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = None
    user_instruction: str = Field(default="", max_length=12000)


class ResumeGenerationPayload(BaseModel):
    generation_attempt_id: str = Field(min_length=1, max_length=64)


class ContractChatPayload(BaseModel):
    message: str = Field(min_length=1)
    allow_draft_changes: bool = False
    expected_draft_revision: int | None = Field(default=None, ge=0)
    selected_version: int | None = Field(default=None, ge=1)
    profile: str = "high"
    idempotency_key: str | None = Field(default=None, max_length=128)


class DraftPatch(BaseModel):
    section_key: str
    content: str
    expected_revision: int | None = Field(default=None, ge=0)
    base_version: int | None = Field(default=None, ge=1)


class DraftFragmentPatch(BaseModel):
    section_key: str
    fragment_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    content: str = ""
    expected_revision: int | None = Field(default=None, ge=0)
    base_version: int | None = Field(default=None, ge=1)


class ExecutionPolicyPatch(BaseModel):
    model_tier: str
    duration_tier: str


class DraftStartPayload(BaseModel):
    base_version: int | None = Field(default=None, ge=1)


class ClonePayload(BaseModel):
    version: int = Field(ge=1)


class ApprovePayload(BaseModel):
    target_kind: str = "immutable_version"
    version: int | None = Field(default=None, ge=1)
    expected_draft_revision: int | None = Field(default=None, ge=0)
    acknowledge_historical_with_draft: bool = False
    research_language: Literal["zh", "en"] | None = None


class PreferencesPayload(BaseModel):
    display_name: str | None = None
    institution: str | None = Field(default=None, max_length=240)
    department: str | None = Field(default=None, max_length=240)
    position: str | None = Field(default=None, max_length=160)
    default_model_strategy: str | None = None
    default_budget_preset: str | None = None
    website_language: Literal["zh", "en"] | None = None
    default_contract_language: Literal["zh", "en"] | None = None
    default_research_language: Literal["zh", "en"] | None = None
    contract_chat_panel_width: float | None = None


class DevSwitchPayload(BaseModel):
    user_id: str


class OtpRequestPayload(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class OtpVerifyPayload(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=6, max_length=12)


class AdminUserPatch(BaseModel):
    role: str | None = None
    is_developer: bool | None = None
    status: str | None = None
    monthly_cost_limit: float | None = None
    storage_quota_bytes: int | None = Field(default=None, ge=0)
    max_concurrent_research_runs: int | None = None
    default_model_strategy: str | None = None
    default_budget_preset: str | None = None


class CreditGrantPayload(BaseModel):
    amount: float
    reason: str = "admin grant"


class DomainPayload(BaseModel):
    domain: str
    group_id: str | None = None
    welcome_credit: float = 0.0
    grant_key: str | None = None
    enabled: bool = True


class PendingUserCreatePayload(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=160)
    is_developer: bool = False
    storage_quota_bytes: int = Field(default=536_870_912, ge=0)
    group_ids: list[str] = Field(default_factory=list)
    initial_credit: float = 0.0


class KeepRuntimePayload(BaseModel):
    keep: bool


class GroupCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    default_monthly_cost_limit: float | None = None
    default_max_concurrent_runs: int | None = None
    default_model_strategy: str | None = None
    default_budget_preset: str | None = None


class GroupPatchPayload(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    default_monthly_cost_limit: float | None = None
    default_max_concurrent_runs: int | None = None
    default_model_strategy: str | None = None
    default_budget_preset: str | None = None


class GroupMemberPayload(BaseModel):
    user_id: str


class GroupMembersReplacePayload(BaseModel):
    user_ids: list[str] = Field(default_factory=list)


class UserGroupsReplacePayload(BaseModel):
    group_ids: list[str] = Field(default_factory=list)


class GroupCreditPayload(BaseModel):
    amount: float
    reason: str = "group grant"


class VNextApplication:
    """装配 vNext 数据、Contractor 与 Dynamic Prover 的运行对象。"""

    def __init__(
        self,
        settings: VNextSettings,
        *,
        database: Database | None = None,
        artifacts: ArtifactStore | None = None,
        contract_backend: ContractCodexBackend | None = None,
        prover_backend: ProverCodexBackend | None = None,
    ) -> None:
        self.settings = settings
        self.database = database or Database(
            settings.database_url, echo=settings.database_echo
        )
        self.artifacts = artifacts or ArtifactStore(settings.data_root)
        self.planner_config = load_planner_config(settings.planner_config)
        self.prover_config = load_prover_config(settings.prover_config)
        if contract_backend is not None:
            self.contract_backend = contract_backend
        elif settings.offline_agents:
            self.contract_backend = OfflineContractBackend()
        else:
            self.contract_backend = ContractCodexBackend(
                self.planner_config,
                settings.root / "planner/prompts/contract",
                self.artifacts.platform_root / "services" / "contractor",
            )
        if prover_backend is not None:
            self.prover_backend = prover_backend
        elif settings.offline_agents:
            self.prover_backend = OfflineProverBackend()
        else:
            self.prover_backend = ProverCodexBackend(
                self.prover_config,
                settings.root / "prover/prompts/vnext",
                self.artifacts.platform_root / "services" / "prover",
                compute_timeout_seconds=settings.compute_tool_timeout_seconds,
            )
        self.auth = AuthService(settings, self.database, self.artifacts)
        self.contracts = ContractService(
            settings,
            self.database,
            self.artifacts,
            self.contract_backend,
            prover_model_profiles(self.prover_config),
        )
        self.prover = DynamicResearchController(
            settings, self.database, self.artifacts, self.prover_backend
        )
        self._combined_generation_stop = asyncio.Event()
        self._combined_generation_task: asyncio.Task[None] | None = None
        self._combined_generation_active: dict[str, asyncio.Task[None]] = {}
        self._combined_run_active: dict[str, asyncio.Task[None]] = {}
        self._combined_deletion_active: dict[str, asyncio.Task[None]] = {}
        self._chat_cleanup_tasks: set[asyncio.Task[None]] = set()
        self.deletions = DeletionExecutor(self.database, self.artifacts, self.prover, self._combined_generation_active)
        self.runtime_gc = RuntimeGcService(
            self.database, self.artifacts, self.settings.runner_id + "-combined",
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
            if self.settings.test_auth_bypass and self.settings.seed_dev_users:
                repo.ensure_dev_users()
            repo.sync_role_run_limits(
                admin_limit=self.settings.developer_max_concurrent_runs,
                user_limit=self.settings.user_max_concurrent_runs,
            )
            if self.settings.process_role == "combined":
                repo.release_stale_run_claims()
                repo.release_stale_generation_claims()
                repo.release_stale_deletion_claims()
        # Web owns short Contract Chat turns; combined mode also owns background
        # Contract generation. Production long Contract generation is runner-owned.
        if self.settings.process_role in {"combined", "web"}:
            await self.contract_backend.start()
        await self.prover.start()
        if self.settings.process_role == "combined":
            self._combined_generation_stop.clear()
            self._combined_generation_task = asyncio.create_task(
                self._combined_generation_loop(), name="mathlab-combined-contract-generation"
            )

    async def stop(self) -> None:
        self._combined_generation_stop.set()
        if self._combined_generation_task is not None:
            await self._combined_generation_task
            self._combined_generation_task = None
        if self._combined_generation_active:
            tasks = list(self._combined_generation_active.values())
            done, pending = await asyncio.wait(
                tasks, timeout=max(0.1, float(self.settings.graceful_shutdown_soft_seconds))
            )
            del done
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._combined_generation_active.clear()
        if self._combined_deletion_active:
            await asyncio.gather(*self._combined_deletion_active.values(), return_exceptions=True)
            self._combined_deletion_active.clear()
        for task in self._chat_cleanup_tasks:
            task.cancel()
        if self._chat_cleanup_tasks:
            await asyncio.gather(*self._chat_cleanup_tasks, return_exceptions=True)
            self._chat_cleanup_tasks.clear()
        await self.prover.stop()
        if self._combined_run_active:
            await asyncio.gather(*self._combined_run_active.values(), return_exceptions=True)
            self._combined_run_active.clear()
        if self.settings.process_role in {"combined", "web"}:
            await self.contract_backend.stop()
        self.database.dispose()

    def schedule_chat_stream_cleanup(self, contract_version_id: str, stream_id: str) -> None:
        """终态保留一分钟供断线续读，之后只清除高频 delta 事件。"""
        async def cleanup() -> None:
            await asyncio.sleep(60)
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).purge_contract_chat_stream_deltas(
                    contract_version_id, stream_id
                )

        task = asyncio.create_task(cleanup(), name=f"chat-stream-cleanup-{stream_id}")
        self._chat_cleanup_tasks.add(task)
        task.add_done_callback(self._chat_cleanup_tasks.discard)

    async def _combined_generation_loop(self) -> None:
        while not self._combined_generation_stop.is_set():
            for mapping in (
                self._combined_generation_active,
                self._combined_run_active,
                self._combined_deletion_active,
            ):
                for key, task in list(mapping.items()):
                    if task.done():
                        try:
                            task.result()
                        except (asyncio.CancelledError, Exception):
                            pass
                        mapping.pop(key, None)
            if not self._combined_deletion_active:
                with self.database.session() as session:
                    job = RepositoryHub(session, self.artifacts).claim_next_deletion_job(self.settings.runner_id + "-combined")
                if job is not None:
                    self._combined_deletion_active[job.deletion_job_id] = asyncio.create_task(
                        self.deletions.execute(job.deletion_job_id, timeout=float(self.settings.graceful_shutdown_hard_seconds)),
                        name=f"combined-delete-{job.deletion_job_id}",
                    )
            if not self._combined_deletion_active:
                if time.monotonic() - self._last_runtime_gc >= self.settings.runtime_gc_poll_seconds:
                    await asyncio.to_thread(self.runtime_gc.sweep_once)
                    self._last_runtime_gc = time.monotonic()
                while True:
                    with self.database.session() as session:
                        attempt = RepositoryHub(session, self.artifacts).claim_next_generation_attempt(
                            self.settings.runner_id + "-combined",
                            admin_limit=self.settings.admin_contract_generation_max_concurrent,
                            user_limit=self.settings.user_contract_generation_max_concurrent,
                        )
                    if attempt is None:
                        break
                    task = asyncio.create_task(
                        self.contracts.execute_generation_attempt(attempt.generation_attempt_id),
                        name=f"combined-contract-generation-{attempt.generation_attempt_id}",
                    )
                    self._combined_generation_active[attempt.generation_attempt_id] = task
                while True:
                    with self.database.session() as session:
                        repo = RepositoryHub(session, self.artifacts)
                        run = repo.claim_next_queued_run(
                            self.settings.runner_id + "-combined",
                            admin_limit=self.settings.admin_runner_max_concurrent_runs,
                            user_limit=self.settings.user_runner_max_concurrent_runs,
                        )
                        if run is None or not run.attempts:
                            break
                        run_id = run.run_id
                        owner_user_id = run.owner_user_id
                        attempt_id = run.attempts[-1].attempt_id
                    self.prover.launch_run(owner_user_id, run_id, attempt_id)
                    self._combined_run_active[run_id] = asyncio.create_task(
                        self.prover.wait_run(run_id),
                        name=f"combined-wait-{run_id}",
                    )
            try:
                await asyncio.wait_for(
                    self._combined_generation_stop.wait(), timeout=self.settings.runner_poll_seconds
                )
            except TimeoutError:
                pass



def create_vnext_app(
    settings: VNextSettings,
    *,
    runtime: VNextApplication | None = None,
) -> FastAPI:
    runtime = runtime or VNextApplication(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title=settings.name, lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Double-submit CSRF protection for production cookie-authenticated writes.

        OTP request/verification are pre-authentication endpoints and are exempt.  In
        development mode the explicit local user switcher remains available and CSRF
        is disabled to keep deterministic product tests simple.
        """
        unsafe = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
        exempt = request.url.path in {"/api/auth/request-otp", "/api/auth/verify-otp"}
        if unsafe and not exempt and not settings.test_auth_bypass and request.url.path.startswith("/api/"):
            cookie = request.cookies.get(settings.csrf_cookie_name, "")
            header = request.headers.get("X-CSRF-Token", "")
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                return Response(status_code=403, content="CSRF validation failed")
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
    browser_root = settings.root / "browser"
    app.mount("/static", StaticFiles(directory=browser_root), name="static")

    def optional_session_user(request: Request) -> str | None:
        session_id = request.cookies.get(settings.session_cookie_name)
        if not session_id:
            return None
        try:
            return runtime.auth.resolve_session(session_id)
        except Exception:
            return None

    @app.get("/")
    async def index(request: Request):
        if not settings.test_auth_bypass and optional_session_user(request) is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(browser_root / "index.html")

    @app.get("/login")
    async def login_page(request: Request):
        if optional_session_user(request) is not None:
            return RedirectResponse("/", status_code=302)
        return FileResponse(browser_root / "login.html")

    def current_user_id(
        request: Request,
        x_mathlab_user: str | None = Header(default=None),
        mathlab_dev_user: str | None = Cookie(default=None),
    ) -> str:
        try:
            if settings.test_auth_bypass:
                user_id = x_mathlab_user or mathlab_dev_user or "dev_admin"
                with runtime.database.session() as session:
                    RepositoryHub(session, runtime.artifacts).get_user(user_id)
                return user_id
            session_id = request.cookies.get(settings.session_cookie_name)
            if not session_id:
                raise OwnershipError("需要登录")
            return runtime.auth.resolve_session(session_id)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def api_error(exc: Exception, *, default_status: int = 409) -> HTTPException:
        if isinstance(exc, OwnershipError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, FileNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=default_status, detail=str(exc))
        return HTTPException(status_code=default_status, detail=str(exc))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = browser_root / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            from sqlalchemy import text
            session.execute(text("SELECT 1"))
            heartbeats = []
            try:
                from .db import RunnerHeartbeatRecord
                from sqlalchemy import select
                heartbeats = [
                    {"runner_id": item.runner_id, "status": item.status, "current_run_id": item.current_run_id, "last_heartbeat_at": utc_iso(item.last_heartbeat_at)}
                    for item in session.scalars(select(RunnerHeartbeatRecord))
                ]
            except Exception:
                heartbeats = []
        return {
            "status": "ok",
            "process_role": settings.process_role,
            "offline": settings.offline_agents,
            "build_sha": os.environ.get("MATHLAB_BUILD_SHA", "development"),
            "runners": heartbeats,
        }

    @app.post("/api/auth/request-otp")
    async def request_otp(payload: OtpRequestPayload) -> dict[str, Any]:
        try:
            result = runtime.auth.request_otp(payload.email)
            body = {"email": result.email, "expires_in_seconds": result.expires_in_seconds}
            if result.dev_code is not None:
                body["dev_code"] = result.dev_code
            return body
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/auth/verify-otp")
    async def verify_otp(payload: OtpVerifyPayload, response: Response) -> dict[str, Any]:
        try:
            user_id, session_id = runtime.auth.verify_otp(payload.email, payload.code)
            csrf_token = secrets.token_urlsafe(32)
            response.set_cookie(
                settings.session_cookie_name, session_id, httponly=True,
                secure=settings.session_cookie_secure, samesite="lax",
                max_age=settings.session_ttl_seconds, path="/",
            )
            response.set_cookie(
                settings.csrf_cookie_name, csrf_token, httponly=False,
                secure=settings.session_cookie_secure, samesite="lax",
                max_age=settings.session_ttl_seconds, path="/",
            )
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                user = repo.get_user(user_id)
                return _user_payload(user, groups=repo.list_user_groups(user_id))
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        session_id = request.cookies.get(settings.session_cookie_name)
        if session_id:
            runtime.auth.logout(session_id)
        response.delete_cookie(settings.session_cookie_name, path="/")
        response.delete_cookie(settings.csrf_cookie_name, path="/")
        return {"logged_out": True}

    @app.get("/api/auth/me")
    async def auth_me(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            return _user_payload(repo.get_user(user_id), groups=repo.list_user_groups(user_id))

    # ---------- Admin: users, domains, groups and credit ----------
    @app.get("/api/admin/users")
    async def admin_users(email: str | None = Query(default=None), user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            repo.require_admin(user_id)
            users = []
            reference = utc_now()
            for item in repo.list_users(email):
                usage = repo.usage_summary(item.user_id)  # All-time UsageLedger, including unsettled turns.
                consumed = repo.credit_consumption_windows(item.user_id, now=reference)
                users.append({
                    **_user_payload(item, groups=repo.list_user_groups(item.user_id)),
                    **usage,
                    "total_consumed_credit": consumed["all"],
                    "consumed_credit_windows": consumed,
                })
            return users

    @app.post("/api/admin/users", status_code=201)
    async def admin_create_pending_user(payload: PendingUserCreatePayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                item = repo.precreate_pending_user(
                    user_id, payload.email, display_name=payload.display_name,
                    is_developer=payload.is_developer,
                    storage_quota_bytes=payload.storage_quota_bytes,
                )
                for group_id in payload.group_ids:
                    repo.add_group_member(user_id, group_id, item.user_id)
                if payload.initial_credit:
                    repo.grant_credit(user_id, item.user_id, payload.initial_credit, "pending user initial grant")
                return _user_payload(item, groups=repo.list_user_groups(item.user_id))
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.delete("/api/admin/users/{target_user_id}")
    async def admin_delete_pending_user(target_user_id: str, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).delete_empty_pending_user(user_id, target_user_id)
            return {"deleted": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.patch("/api/admin/users/{target_user_id}")
    async def admin_update_user(target_user_id: str, payload: AdminUserPatch, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                item = repo.update_user_admin(
                    user_id, target_user_id, role=payload.role,
                    is_developer=payload.is_developer, status=payload.status,
                    monthly_cost_limit=payload.monthly_cost_limit,
                    storage_quota_bytes=payload.storage_quota_bytes,
                    max_concurrent_research_runs=(
                        payload.max_concurrent_research_runs
                        if payload.max_concurrent_research_runs is not None
                        else (
                            settings.developer_max_concurrent_runs
                            if payload.role == "admin"
                            else settings.user_max_concurrent_runs
                            if payload.role == "user"
                            else None
                        )
                    ),
                    default_model_strategy=payload.default_model_strategy,
                    default_budget_preset=payload.default_budget_preset,
                )
                return _user_payload(item, groups=repo.list_user_groups(item.user_id))
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.put("/api/admin/users/{target_user_id}/groups")
    async def admin_replace_user_groups(
        target_user_id: str,
        payload: UserGroupsReplacePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                changes = repo.replace_user_custom_groups(
                    user_id, target_user_id, payload.group_ids
                )
                target = repo.get_user_any_status(target_user_id)
                return {
                    **changes,
                    "user": _user_payload(
                        target, groups=repo.list_user_groups(target_user_id)
                    ),
                }
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/admin/users/{target_user_id}/credit")
    async def admin_grant_credit(target_user_id: str, payload: CreditGrantPayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                entry = repo.grant_credit(user_id, target_user_id, payload.amount, payload.reason)
                target = repo.get_user_any_status(target_user_id)
                return {"credit_event_id": entry.credit_event_id, "credit_balance": target.credit_balance}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/admin/domains")
    async def admin_domains(user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            repo.require_admin(user_id)
            return [
                {
                    "domain_id": item.rule_id, "domain": item.domain,
                    "group_id": item.group_id, "welcome_credit": float(item.welcome_credit),
                    "grant_key": item.grant_key, "enabled": item.enabled,
                }
                for item in repo.list_domain_group_rules()
            ]

    @app.post("/api/admin/domains")
    async def admin_domain_upsert(payload: DomainPayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                group_id = payload.group_id
                if not group_id:
                    external = next((item for item in repo.list_groups() if item.system_key == "external"), None)
                    if external is None:
                        raise FileNotFoundError("External Group 不存在")
                    group_id = external.group_id
                item = repo.upsert_domain_group_rule(
                    user_id, domain=payload.domain, group_id=group_id,
                    welcome_credit=payload.welcome_credit,
                    enabled=payload.enabled, grant_key=payload.grant_key,
                )
                return {"domain_id": item.rule_id, "domain": item.domain, "group_id": item.group_id, "welcome_credit": float(item.welcome_credit), "enabled": item.enabled}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.delete("/api/admin/domains/{domain_id}")
    async def admin_domain_delete(domain_id: str, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).delete_domain_group_rule(user_id, domain_id)
            return {"deleted": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/admin/groups")
    async def admin_groups(user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            repo.require_admin(user_id)
            return [
                _group_payload(
                    item,
                    members=[_user_payload(user) for user in repo.list_group_members(item.group_id)],
                    membership_mode=(
                        "automatic" if repo.group_membership_is_automatic(item.group_id)
                        else "manual"
                    ),
                )
                for item in repo.list_groups()
            ]

    @app.post("/api/admin/groups")
    async def admin_group_create(payload: GroupCreatePayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                item = repo.create_group(user_id, payload.name, payload.description)
                item = repo.update_group(
                    user_id, item.group_id,
                    default_monthly_cost_limit=payload.default_monthly_cost_limit,
                    default_max_concurrent_runs=payload.default_max_concurrent_runs,
                    default_model_strategy=payload.default_model_strategy,
                    default_budget_preset=payload.default_budget_preset,
                )
                return _group_payload(item, members=[])
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.patch("/api/admin/groups/{group_id}")
    async def admin_group_update(group_id: str, payload: GroupPatchPayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                item = repo.update_group(
                    user_id, group_id, **payload.model_dump(exclude_none=True)
                )
                return _group_payload(
                    item,
                    members=[_user_payload(user) for user in repo.list_group_members(group_id)],
                    membership_mode=(
                        "automatic" if repo.group_membership_is_automatic(group_id)
                        else "manual"
                    ),
                )
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.delete("/api/admin/groups/{group_id}")
    async def admin_group_delete(group_id: str, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).delete_group(user_id, group_id)
            return {"deleted": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/admin/groups/{group_id}/members")
    async def admin_group_add_member(group_id: str, payload: GroupMemberPayload, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).add_group_member(user_id, group_id, payload.user_id)
            return {"added": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.put("/api/admin/groups/{group_id}/members")
    async def admin_replace_group_members(
        group_id: str,
        payload: GroupMembersReplacePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                changes = repo.replace_group_members(user_id, group_id, payload.user_ids)
                group = next(
                    (item for item in repo.list_groups() if item.group_id == group_id),
                    None,
                )
                if group is None:
                    raise FileNotFoundError("Group 不存在")
                return {
                    **changes,
                    "group": _group_payload(
                        group,
                        members=[
                            _user_payload(member)
                            for member in repo.list_group_members(group_id)
                        ],
                        membership_mode="manual",
                    ),
                }
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.delete("/api/admin/groups/{group_id}/members/{target_user_id}")
    async def admin_group_remove_member(group_id: str, target_user_id: str, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).remove_group_member(user_id, group_id, target_user_id)
            return {"removed": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/admin/groups/{group_id}/credit")
    async def admin_group_credit(group_id: str, payload: GroupCreditPayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                count = RepositoryHub(session, runtime.artifacts).grant_group_credit(user_id, group_id, payload.amount, payload.reason)
            return {"members_updated": count}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/admin/credit-ledger")
    async def admin_credit_ledger(target_user_id: str | None = Query(default=None), user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            repo.require_admin(user_id)
            return [
                {
                    "credit_event_id": item.credit_event_id, "user_id": item.user_id,
                    "group_id": item.group_id, "amount": item.amount, "reason": item.reason,
                    "created_by": item.created_by, "run_id": item.run_id,
                    "created_at": utc_iso(item.created_at),
                }
                for item in repo.list_credit_ledger(target_user_id)
            ]

    @app.get("/api/admin/audit-log")
    async def admin_audit_log(limit: int = Query(default=200, ge=1, le=1000), user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            return [
                {
                    "audit_id": item.audit_id, "actor_user_id": item.actor_user_id,
                    "action": item.action, "target_type": item.target_type,
                    "target_id": item.target_id, "payload": item.payload,
                    "created_at": utc_iso(item.created_at),
                }
                for item in repo.list_admin_audit(user_id, limit=limit)
            ]

    # ---------- Developer: raw runtime and retention ----------
    @app.get("/api/developer/runs/{run_id}/runtime")
    async def developer_runtime_tree(run_id: str, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            repo.require_developer(user_id)
            from .db import ResearchRunRecord
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Research Run 不存在")
            root = runtime.artifacts.resolve(run.run_root) / "runtime"
            files = []
            if root.is_dir():
                for path in sorted(item for item in root.rglob("*") if item.is_file())[:5000]:
                    files.append({
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                    })
            repo.add_admin_audit(user_id, "developer_runtime_list", "research_run", run_id, {"files": len(files)})
            return {
                "run_id": run_id, "keep_runtime": run.keep_runtime,
                "runtime_expires_at": utc_iso(run.runtime_expires_at),
                "runtime_cleaned_at": utc_iso(run.runtime_cleaned_at),
                "files": files,
            }

    @app.get("/api/developer/runs/{run_id}/runtime/file")
    async def developer_runtime_file(run_id: str, path: str = Query(min_length=1), user_id: str = Depends(current_user_id)) -> FileResponse:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts); repo.require_developer(user_id)
            from .db import ResearchRunRecord
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Research Run 不存在")
            root = (runtime.artifacts.resolve(run.run_root) / "runtime").resolve()
            target = (root / path).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise HTTPException(status_code=404, detail="Runtime 文件不存在")
            repo.add_admin_audit(user_id, "developer_runtime_read", "research_run", run_id, {"path": path})
        return FileResponse(target, filename=target.name)

    @app.patch("/api/developer/runs/{run_id}/keep-runtime")
    async def developer_keep_runtime(run_id: str, payload: KeepRuntimePayload, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts); repo.require_developer(user_id)
        item = runtime.runtime_gc.set_keep_runtime(run_id, payload.keep)
        with runtime.database.session() as session:
            RepositoryHub(session, runtime.artifacts).add_admin_audit(
                user_id, "developer_keep_runtime", "research_run", run_id, {"keep": payload.keep}
            )
        return {"run_id": run_id, "keep_runtime": item.keep_runtime}

    @app.post("/api/developer/runs/{run_id}/runtime-gc/dry-run")
    async def developer_gc_dry_run(run_id: str, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts); repo.require_developer(user_id)
            repo.add_admin_audit(user_id, "developer_runtime_gc_dry_run", "research_run", run_id)
        return runtime.runtime_gc.dry_run(run_id)

    @app.get("/api/bootstrap")
    async def bootstrap(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        selected = user_id
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            if settings.test_auth_bypass and settings.seed_dev_users:
                repo.ensure_dev_users()
            user = repo.get_user(selected)
            projects = repo.list_projects(user.user_id)
            conversations = repo.list_conversations(user.user_id)
            files = repo.list_file_assets(user.user_id)
            usage = repo.usage_summary(user.user_id, since=_usage_window_since("7d"))
            usage["window"] = "7d"
            users = repo.list_users() if settings.test_auth_bypass else []
            deletion_jobs = repo.list_active_deletion_jobs(user.user_id)
            project_deletions = {item.target_id: item for item in deletion_jobs if item.target_type == "project"}
            conversation_deletions = {item.target_id: item for item in deletion_jobs if item.target_type == "conversation"}
            run_events = _active_run_events(repo, conversations)
            return {
                "application": {
                    "name": settings.name,
                    "dev_mode": settings.test_auth_bypass,
                    "contract_sections": [
                        {"key": key, "level": level, "heading": heading, "editable": key in EDITABLE_KEYS}
                        for key, level, heading in SECTION_SPECS
                    ],
                },
                "current_user": _user_payload(user, groups=repo.list_user_groups(user.user_id)),
                "dev_users": [_user_payload(item) for item in users],
                "projects": [_project_payload(item, project_deletions.get(item.project_id)) for item in projects],
                "conversations": [
                    _conversation_list_payload(
                        item,
                        conversation_deletions.get(item.conversation_id)
                        or project_deletions.get(item.project_id),
                        runtime_phase=_conversation_runtime_phase(item, run_events),
                    )
                    for item in conversations
                ],
                "recent_research": _recent_research_payload(repo, conversations, run_events),
                "files": [_file_payload(item) for item in files],
                "usage": usage,
                "contract_generation_catalog": {
                    profile: {
                        "precharge_credit": settings.contract_reserve(profile),
                        "estimated_minutes": int(settings.contract_generation_estimated_minutes.get(profile, 20)),
                    }
                    for profile in ("low", "medium", "high")
                },
                "execution_policy_catalog": runtime.contracts.execution_policy_catalog(),
            }

    @app.get("/api/research-overview")
    async def research_overview(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        """轻量返回首页与左栏所需的最新 Run 状态，不扫描 Usage 或存储。"""
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            conversations = repo.list_conversations(user_id)
            run_events = _active_run_events(repo, conversations)
            return {
                "conversations": [
                    {
                        "conversation_id": item.conversation_id,
                        "status": item.status,
                        "run_id": item.research_run.run_id if item.research_run else None,
                        "run_status": item.research_run.status if item.research_run else None,
                        "runtime_phase": _conversation_runtime_phase(item, run_events),
                    }
                    for item in conversations
                ],
                "recent_research": _recent_research_payload(repo, conversations, run_events),
            }

    @app.post("/api/dev/switch-user")
    async def switch_user(payload: DevSwitchPayload, response: Response) -> dict[str, Any]:
        if not settings.test_auth_bypass:
            raise HTTPException(status_code=404, detail="仅开发模式可切换用户")
        with runtime.database.session() as session:
            user = RepositoryHub(session, runtime.artifacts).get_user(payload.user_id)
        response.set_cookie(
            "mathlab_dev_user",
            user.user_id,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 24 * 30,
        )
        return _user_payload(user)

    @app.patch("/api/users/me/preferences")
    async def update_preferences(
        payload: PreferencesPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        with runtime.database.session() as session:
            repo = RepositoryHub(session, runtime.artifacts)
            user = repo.update_user_preferences(
                user_id,
                display_name=payload.display_name,
                institution=payload.institution,
                department=payload.department,
                position=payload.position,
                default_model_strategy=payload.default_model_strategy,
                default_budget_preset=payload.default_budget_preset,
                website_language=payload.website_language,
                default_contract_language=payload.default_contract_language,
                default_research_language=payload.default_research_language,
                contract_chat_panel_width=payload.contract_chat_panel_width,
            )
            return _user_payload(user)

    @app.get("/api/users/me/usage")
    async def usage(
        window: str = Query("7d", pattern="^(1d|7d|30d|all)$"),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        with runtime.database.session() as session:
            result = RepositoryHub(session, runtime.artifacts).usage_summary(
                user_id, since=_usage_window_since(window)
            )
            result["window"] = window
            return result

    # ---------- Projects: organization only ----------
    @app.post("/api/projects")
    async def create_project(
        payload: ProjectCreate,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        with runtime.database.session() as session:
            item = RepositoryHub(session, runtime.artifacts).create_project(
                user_id, payload.name, payload.description
            )
            return _project_payload(item)

    @app.patch("/api/projects/{project_id}")
    async def rename_project(
        project_id: str,
        payload: RenamePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).rename_project(
                    user_id, project_id, payload.name
                )
                return _project_payload(item)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.delete("/api/projects/{project_id}")
    async def delete_project(
        project_id: str,
        response: Response,
        confirm: str = Query(default=""),
        terminate_active: bool = Query(default=False),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        if confirm != "permanently-delete-project-and-all-chats-and-tasks":
            raise HTTPException(status_code=400, detail="缺少永久删除确认")
        try:
            with runtime.database.session() as session:
                deleted, job = RepositoryHub(session, runtime.artifacts).request_deletion(
                    user_id, "project", project_id, terminate_active=terminate_active
                )
            if job is not None:
                response.status_code = 202
                return {"deleted": False, "deletion_job_id": job.deletion_job_id, "status": job.status}
            return {"deleted": deleted}
        except Exception as exc:
            raise api_error(exc) from exc

    # ---------- User file library ----------
    @app.get("/api/files")
    async def list_files(user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
        with runtime.database.session() as session:
            return [_file_payload(item) for item in RepositoryHub(session, runtime.artifacts).list_file_assets(user_id)]

    @app.post("/api/files")
    async def upload_file(
        file: UploadFile = File(...),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        payload = await file.read()
        if len(payload) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="单个文件不得超过 50 MiB")
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).create_file_asset(
                    user_id,
                    name=file.filename or "upload.txt",
                    media_type=file.content_type or "application/octet-stream",
                    payload=payload,
                )
                return _file_payload(item)
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/files/{file_id}/download")
    async def download_file(
        file_id: str,
        user_id: str = Depends(current_user_id),
    ) -> FileResponse:
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).get_file_asset(user_id, file_id)
                path = runtime.artifacts.resolve(item.artifact_path)
                name = item.original_name
                media_type = item.media_type
            return FileResponse(path, filename=name, media_type=media_type)
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.patch("/api/files/{file_id}")
    async def rename_file(
        file_id: str,
        payload: RenamePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """重命名当前用户的文件显示名，不改内容、哈希或历史快照。"""
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).rename_file_asset(
                    user_id, file_id, payload.name
                )
                return _file_payload(item)
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.delete("/api/files/{file_id}")
    async def delete_file(
        file_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, bool]:
        try:
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).delete_file_asset(user_id, file_id)
            return {"deleted": True}
        except Exception as exc:
            raise api_error(exc) from exc

    # ---------- Contract Request Drafts / Conversations ----------
    @app.post("/api/contract-drafts", status_code=201)
    async def create_request_draft(
        payload: RequestDraftPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.save_request_draft(
                user_id,
                conversation_id=None,
                expected_revision=0,
                research_problem=payload.research_problem,
                background_and_prior_progress=payload.background_and_prior_progress,
                method_preferences_and_restrictions=payload.method_preferences_and_restrictions,
                valuable_partial_outcomes=payload.valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=payload.possible_ideas_and_rough_approaches,
                contractor_profile=payload.contractor_profile,
                contract_language=payload.contract_language,
                project_id=payload.project_id,
                source_file_ids=payload.source_file_ids,
                display_name=payload.display_name,
            )
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.patch("/api/contract-drafts/{conversation_id}")
    async def update_request_draft(
        conversation_id: str,
        payload: RequestDraftPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.save_request_draft(
                user_id,
                conversation_id=conversation_id,
                expected_revision=payload.expected_revision,
                research_problem=payload.research_problem,
                background_and_prior_progress=payload.background_and_prior_progress,
                method_preferences_and_restrictions=payload.method_preferences_and_restrictions,
                valuable_partial_outcomes=payload.valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=payload.possible_ideas_and_rough_approaches,
                contractor_profile=payload.contractor_profile,
                contract_language=payload.contract_language,
                project_id=payload.project_id,
                source_file_ids=payload.source_file_ids,
                display_name=payload.display_name,
            )
        except Exception as exc:
            status = 409 if "DraftRevisionConflict" in str(exc) else 400
            raise api_error(exc, default_status=status) from exc

    @app.post("/api/conversations")
    async def create_conversation_compat(
        payload: ConversationCreate,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """Compatibility entry point; the product UI uses /api/contract-drafts."""
        try:
            problem = payload.research_problem or payload.research_request
            draft = runtime.contracts.save_request_draft(
                user_id,
                conversation_id=None,
                expected_revision=0,
                research_problem=problem,
                background_and_prior_progress=payload.background_and_prior_progress,
                method_preferences_and_restrictions=payload.method_preferences_and_restrictions,
                valuable_partial_outcomes=payload.valuable_partial_outcomes,
                possible_ideas_and_rough_approaches=payload.possible_ideas_and_rough_approaches,
                contractor_profile=payload.profile,
                contract_language=payload.contract_language,
                project_id=payload.project_id,
                source_file_ids=payload.source_file_ids,
                display_name=payload.display_name,
            )
            generation = None
            generated = None
            if payload.generate_contract:
                # Compatibility-only synchronous path used by legacy integrations and
                # deterministic tests.  The product UI always uses the DB-backed
                # /generate endpoint and returns immediately.
                generated_result = await runtime.contracts.generate_initial(
                    user_id, draft["conversation_id"]
                )
                generated = _generated_payload(generated_result)
                generation = runtime.contracts.generation_status(user_id, draft["conversation_id"])
            return {
                "conversation_id": draft["conversation_id"],
                "draft": draft,
                "generation": generation,
                "generated": generated,
            }
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return _conversation_detail(runtime, user_id, conversation_id)
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: str,
        payload: RenamePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        with runtime.database.session() as session:
            item = RepositoryHub(session, runtime.artifacts).rename_conversation(
                user_id, conversation_id, payload.name
            )
            return _conversation_list_payload(item)

    @app.post("/api/conversations/{conversation_id}/move")
    async def move_conversation(
        conversation_id: str,
        payload: ConversationMove,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).move_conversation(
                    user_id, conversation_id, payload.project_id
                )
                return _conversation_list_payload(item)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/navigation/reorder")
    async def reorder_navigation(
        payload: NavigationReorderPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """持久化 Project 顺序，或移动并重排一个 Contract Conversation。"""
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                if payload.entity_type == "project":
                    if payload.target_project_id is not None:
                        raise ValueError("Project 不能归档到其他 Project 或 Unfiled")
                    order = repo.reorder_project(
                        user_id, payload.entity_id, payload.target_index
                    )
                elif payload.entity_type == "conversation":
                    order = repo.reorder_conversation(
                        user_id,
                        payload.entity_id,
                        target_project_id=payload.target_project_id,
                        target_index=payload.target_index,
                    )
                else:
                    raise ValueError("entity_type 必须为 project 或 conversation")
                return {
                    "entity_type": payload.entity_type,
                    "entity_id": payload.entity_id,
                    "target_project_id": payload.target_project_id,
                    "order": order,
                }
        except Exception as exc:
            raise api_error(exc) from exc

    @app.put("/api/conversations/{conversation_id}/sources")
    async def set_conversation_sources(
        conversation_id: str,
        payload: ConversationSources,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                conversation = repo.set_conversation_sources(
                    user_id, conversation_id, payload.file_ids
                )
                repo.append_event(
                    "conversation", conversation_id, "source_materials_updated",
                    "Contract Conversation 的 Source Materials 已更新。",
                    {"file_ids": payload.file_ids},
                )
                return {
                    "conversation_id": conversation.conversation_id,
                    "source_manifest": repo.source_manifest(user_id, payload.file_ids),
                }
        except Exception as exc:
            raise api_error(exc) from exc

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        response: Response,
        confirm: str = Query(default=""),
        terminate_active: bool = Query(default=False),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        if confirm != "permanently-delete-contract-conversation":
            raise HTTPException(status_code=400, detail="缺少永久删除确认")
        try:
            with runtime.database.session() as session:
                deleted, job = RepositoryHub(session, runtime.artifacts).request_deletion(
                    user_id, "conversation", conversation_id, terminate_active=terminate_active
                )
            if job is not None:
                response.status_code = 202
                return {"deleted": False, "deletion_job_id": job.deletion_job_id, "status": job.status}
            return {"deleted": deleted}
        except Exception as exc:
            raise api_error(exc) from exc

    @app.delete("/api/conversations/{conversation_id}/versions/{version}")
    async def delete_contract_version(
        conversation_id: str,
        version: int,
        confirm: str = Query(default=""),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        if confirm != "permanently-delete-current-contract-version":
            raise HTTPException(status_code=400, detail="缺少 Contract Version 删除确认")
        try:
            with runtime.database.session() as session:
                return RepositoryHub(session, runtime.artifacts).delete_contract_version(
                    user_id, conversation_id, version
                )
        except Exception as exc:
            raise api_error(exc) from exc

    @app.get("/api/deletion-jobs/{deletion_job_id}")
    async def deletion_job(deletion_job_id: str, user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                item = RepositoryHub(session, runtime.artifacts).get_deletion_job(user_id, deletion_job_id)
                return _deletion_job_payload(item)
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.post("/api/conversations/{conversation_id}/generate", status_code=202)
    async def generate_contract(
        conversation_id: str,
        payload: GeneratePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            queued = runtime.contracts.queue_initial_generation(
                user_id, conversation_id, idempotency_key=payload.idempotency_key
            )
            return queued.__dict__
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.post("/api/conversations/{conversation_id}/regenerate", status_code=202)
    async def regenerate_contract(
        conversation_id: str,
        payload: FullRegenerationPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            queued = runtime.contracts.queue_full_regeneration(
                user_id, conversation_id,
                selected_version=payload.selected_version,
                user_instruction=payload.user_instruction,
                idempotency_key=payload.idempotency_key,
            )
            return queued.__dict__
        except Exception as exc:
            status = 409 if "已有生成任务正在运行" in str(exc) else 400
            raise api_error(exc, default_status=status) from exc

    @app.get("/api/conversations/{conversation_id}/generation")
    async def contract_generation_status(
        conversation_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.generation_status(user_id, conversation_id)
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.post(
        "/api/conversations/{conversation_id}/generation/resume", status_code=202
    )
    async def resume_contract_generation(
        conversation_id: str,
        payload: ResumeGenerationPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            queued = runtime.contracts.resume_generation(
                user_id, conversation_id, payload.generation_attempt_id
            )
            return queued.__dict__
        except Exception as exc:
            raise api_error(exc, default_status=409) from exc

    @app.get("/api/conversations/{conversation_id}/generation-events")
    async def contract_generation_events(
        conversation_id: str,
        attempt_id: str,
        after: int = Query(default=0, ge=0),
        user_id: str = Depends(current_user_id),
    ) -> list[dict[str, Any]]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                attempt = repo.get_generation_attempt(user_id, attempt_id)
                if attempt.conversation_id != conversation_id:
                    raise OwnershipError("Generation Attempt 不属于该 Conversation")
                return [
                    {
                        "sequence": item.sequence,
                        "kind": item.kind,
                        "message": item.message,
                        "payload": item.payload,
                        "created_at": utc_iso(item.created_at),
                    }
                    for item in repo.read_events("contract_generation", attempt_id, after)
                ]
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.post("/api/conversations/{conversation_id}/chat")
    async def contract_chat(
        conversation_id: str,
        payload: ContractChatPayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                selected = repo.get_contract_version(
                    user_id, conversation_id, payload.selected_version
                )
                contract_version_id = selected.contract_version_id
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

        stream_id = f"chatstream_{secrets.token_hex(16)}"
        buffered_delta = ""
        last_flush = time.monotonic()

        def append_stream_event(kind: str, payload_data: dict[str, Any]) -> None:
            """把用户可见 Contract Chat 流写入持久事件序列。"""
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).append_event(
                    "contract_chat",
                    contract_version_id,
                    f"chat_{kind}",
                    "",
                    {"stream_id": stream_id, **payload_data},
                )

        def flush_delta() -> None:
            """把高频 token delta 合并成适合数据库与 SSE 的小批次。"""
            nonlocal buffered_delta, last_flush
            if buffered_delta:
                append_stream_event("delta", {"delta": buffered_delta})
                buffered_delta = ""
                last_flush = time.monotonic()

        async def on_stream_event(kind: str, event_payload: dict[str, Any]) -> None:
            """接收 Reply 投影器事件，绝不向浏览器暴露 Draft Patches。"""
            nonlocal buffered_delta
            if kind == "delta":
                buffered_delta += str(event_payload.get("delta") or "")
                if len(buffered_delta) >= 768 or time.monotonic() - last_flush >= 0.08:
                    flush_delta()
                return
            flush_delta()
            append_stream_event(kind, event_payload)

        try:
            append_stream_event("started", {})
            result = await runtime.contracts.contract_chat(
                user_id, conversation_id,
                message=payload.message,
                allow_draft_changes=payload.allow_draft_changes,
                expected_draft_revision=payload.expected_draft_revision,
                selected_version=payload.selected_version,
                profile=payload.profile,
                on_stream_event=on_stream_event,
                idempotency_key=payload.idempotency_key,
            )
            flush_delta()
            append_stream_event(
                "completed",
                {
                    "reply": result["reply"],
                    "contract_version_id": result["contract_version_id"],
                    "contract_version": result["contract_version"],
                    "draft_changed": bool(result.get("draft_changed")),
                    "draft_revision_before": int(result.get("draft_revision_before") or 0),
                    "draft_revision_after": int(result.get("draft_revision_after") or 0),
                    "changed_section_keys": list(result.get("changed_section_keys") or []),
                },
            )
            runtime.schedule_chat_stream_cleanup(contract_version_id, stream_id)
            return {**result, "stream_id": stream_id}
        except Exception as exc:
            flush_delta()
            append_stream_event("failed", {"error": str(exc)})
            runtime.schedule_chat_stream_cleanup(contract_version_id, stream_id)
            status = 409 if "DraftRevisionConflict" in str(exc) else 400
            raise api_error(exc, default_status=status) from exc

    @app.get("/api/conversations/{conversation_id}/chat/events")
    async def contract_chat_events(
        conversation_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        selected_version: int | None = Query(default=None, ge=1),
        user_id: str = Depends(current_user_id),
    ) -> StreamingResponse:
        """以 SSE 续读已持久化的 Contract Chat Reply 事件。"""
        with runtime.database.session() as session:
            selected = RepositoryHub(session, runtime.artifacts).get_contract_version(
                user_id, conversation_id, selected_version
            )
            contract_version_id = selected.contract_version_id

        async def stream() -> AsyncIterator[str]:
            sequence = after
            keepalive_at = time.monotonic()
            while not await request.is_disconnected():
                with runtime.database.session() as session:
                    events = RepositoryHub(session, runtime.artifacts).read_events(
                        "contract_chat", contract_version_id, sequence
                    )
                    payloads = [
                        {
                            "sequence": item.sequence,
                            "kind": item.kind,
                            "payload": item.payload,
                            "created_at": utc_iso(item.created_at),
                        }
                        for item in events
                    ]
                for payload_item in payloads:
                    sequence = int(payload_item["sequence"])
                    yield (
                        f"id: {sequence}\n"
                        "event: contract_chat\n"
                        f"data: {json.dumps(payload_item, ensure_ascii=False)}\n\n"
                    )
                    keepalive_at = time.monotonic()
                if time.monotonic() - keepalive_at >= 15:
                    yield ": keepalive\n\n"
                    keepalive_at = time.monotonic()
                await asyncio.sleep(0.08)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/{conversation_id}/draft")
    async def start_draft(
        conversation_id: str,
        payload: DraftStartPayload = Body(default=DraftStartPayload()),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.start_working_draft(
                user_id, conversation_id, base_version=payload.base_version
            )
        except Exception as exc:
            raise api_error(exc) from exc

    @app.patch("/api/conversations/{conversation_id}/draft")
    async def patch_draft(
        conversation_id: str,
        payload: DraftPatch,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.patch_working_draft_section(
                user_id, conversation_id, payload.section_key, payload.content,
                expected_revision=payload.expected_revision,
                base_version=payload.base_version,
            )
        except Exception as exc:
            status = 409 if "DraftRevisionConflict" in str(exc) else 400
            raise api_error(exc, default_status=status) from exc

    @app.patch("/api/conversations/{conversation_id}/draft/fragment")
    async def patch_draft_fragment(
        conversation_id: str,
        payload: DraftFragmentPatch,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.patch_working_draft_fragment(
                user_id,
                conversation_id,
                payload.section_key,
                payload.fragment_id,
                payload.title,
                payload.content,
                expected_revision=payload.expected_revision,
                base_version=payload.base_version,
            )
        except Exception as exc:
            status = 409 if "DraftRevisionConflict" in str(exc) else 400
            raise api_error(exc, default_status=status) from exc

    @app.patch("/api/conversations/{conversation_id}/execution-policy")
    async def patch_execution_policy(
        conversation_id: str,
        payload: ExecutionPolicyPatch,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.patch_execution_policy(
                user_id, conversation_id,
                model_tier=payload.model_tier,
                duration_tier=payload.duration_tier,
            )
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/conversations/{conversation_id}/draft/commit")
    async def commit_draft(
        conversation_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return _generated_payload(runtime.contracts.commit_working_draft(user_id, conversation_id))
        except Exception as exc:
            raise api_error(exc) from exc

    @app.delete("/api/conversations/{conversation_id}/draft")
    async def discard_draft(
        conversation_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, bool]:
        try:
            runtime.contracts.discard_working_draft(user_id, conversation_id)
            return {"discarded": True}
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/conversations/{conversation_id}/clone")
    async def clone_conversation(
        conversation_id: str,
        payload: ClonePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.contracts.clone_from_contract(user_id, conversation_id, payload.version)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/conversations/{conversation_id}/approve")
    async def approve_contract(
        conversation_id: str,
        payload: ApprovePayload,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            handoff = runtime.contracts.approve_contract(
                user_id, conversation_id,
                target_kind=payload.target_kind,
                version=payload.version,
                expected_draft_revision=payload.expected_draft_revision,
                acknowledge_historical_with_draft=payload.acknowledge_historical_with_draft,
                research_language=payload.research_language,
            )
            launch = {"run_id": handoff["run_id"], "status": "queued"}
            return {**handoff, "launch": launch}
        except Exception as exc:
            message = str(exc)
            status = 409 if (
                "DraftRevisionConflict" in message
                or "WorkingDraftApprovalWarning" in message
            ) else 400
            raise api_error(exc, default_status=status) from exc

    # ---------- Dynamic Prover ----------
    @app.get("/api/runs/{run_id}")
    async def get_run(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            payload = runtime.prover.run_payload(user_id, run_id)
            payload.pop("workers", None)
            payload.pop("assignments", None)
            return payload
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.get("/api/runs/{run_id}/verified-claims/{claim_id}/proof")
    async def get_verified_claim_proof(
        run_id: str,
        claim_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, str]:
        """按需返回当前用户 Run 内已核验 Claim 的完整证明 Markdown。"""
        try:
            return runtime.prover.verified_claim_proof(user_id, run_id, claim_id)
        except Exception as exc:
            raise api_error(exc, default_status=409) from exc

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        user_id: str = Depends(current_user_id),
    ) -> list[dict[str, Any]]:
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                repo.require_developer(user_id)
                from .db import ResearchRunRecord
                if session.get(ResearchRunRecord, run_id) is None:
                    raise FileNotFoundError("Research Run 不存在")
                return [
                    {
                        "sequence": item.sequence,
                        "kind": item.kind,
                        "message": item.message,
                        "payload": item.payload,
                        "created_at": utc_iso(item.created_at),
                    }
                    for item in repo.read_events("run", run_id, after)
                ]
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.get("/api/runs/{run_id}/public-events")
    async def get_public_run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """只返回 Supervisor/Reviewer 的短摘要；内部 RuntimeEvent 不进入产品事件流。"""
        try:
            with runtime.database.session() as session:
                repo = RepositoryHub(session, runtime.artifacts)
                run = repo.get_run(user_id, run_id)
                public = repo.read_events("public_run", run_id, after)
                if public:
                    return {
                        "events": [_public_run_event_payload(item) for item in public],
                        "next_after": public[-1].sequence,
                    }
                if after > 0 or run.status not in {"completed", "failed", "interrupted"}:
                    return {"events": [], "next_after": after}
                legacy: list[tuple[Any, str, str, str | None]] = []
                for item in repo.read_events("run", run_id):
                    if item.kind == "architecture_review":
                        legacy.append((item, "global_review", "全局研究复盘", None))
                for node in repo.list_nodes(run_id):
                    for item in repo.read_events("node", node.node_id):
                        if item.kind == "supervisor_review":
                            legacy.append(
                                (item, "local_review", "局部研究组形成阶段判断", node.node_id)
                            )
                legacy.sort(key=lambda row: row[0].created_at)
                events = [
                    {
                        "sequence": index,
                        "category": category,
                        "title": title,
                        "summary": _legacy_public_summary(item.message),
                        "node_id": node_id,
                        "tone": "research",
                        "created_at": utc_iso(item.created_at),
                    }
                    for index, (item, category, title, node_id) in enumerate(legacy, start=1)
                ]
                return {"events": events, "next_after": len(events)}
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.post("/api/runs/{run_id}/retry")
    async def retry_run(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        try:
            return runtime.prover.retry_run(user_id, run_id)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/runs/{run_id}/graceful-drain")
    async def graceful_drain_run(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """由 Run owner 请求停止新研究并正常进入论文写作。"""
        try:
            return runtime.prover.request_graceful_drain(user_id, run_id)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/runs/{run_id}/pause")
    async def pause_run(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """请求暂停目标 Run 的全部活动 Agent operation。"""
        try:
            return await runtime.prover.request_pause(user_id, run_id)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        """从 durable checkpoint 继续同一 Run Attempt。"""
        try:
            return runtime.prover.request_resume(user_id, run_id)
        except Exception as exc:
            raise api_error(exc) from exc

    @app.delete("/api/runs/{run_id}")
    async def delete_run(
        run_id: str,
        confirm: str = Query(default=""),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, bool]:
        if confirm != "permanently-delete-research-run":
            raise HTTPException(status_code=400, detail="需要明确确认永久删除 Research Run")
        try:
            with runtime.database.session() as session:
                run = RepositoryHub(session, runtime.artifacts).get_run(user_id, run_id)
                active = run.status in {"created", "queued", "resuming", "running", "draining", "paused"}
            if active:
                await runtime.prover.terminate_run_for_deletion(
                    user_id, run_id, timeout=float(settings.graceful_shutdown_hard_seconds)
                )
            with runtime.database.session() as session:
                RepositoryHub(session, runtime.artifacts).delete_run(user_id, run_id)
            return {"deleted": True}
        except Exception as exc:
            raise api_error(exc, default_status=400) from exc

    @app.get("/api/runs/{run_id}/final-paper")
    async def final_paper(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> FileResponse:
        """以内嵌方式返回当前用户最终 PDF，供网页预览。"""
        try:
            with runtime.database.session() as session:
                run = RepositoryHub(session, runtime.artifacts).get_run(user_id, run_id)
                if not run.final_paper_pdf_path:
                    raise FileNotFoundError("最终 PDF 尚未生成")
                path = runtime.artifacts.resolve(run.final_paper_pdf_path)
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=f"{run_id}-paper.pdf",
                content_disposition_type="inline",
            )
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.get("/api/runs/{run_id}/final-paper/download")
    async def download_final_paper(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> FileResponse:
        """下载当前用户最终 PDF。"""
        try:
            with runtime.database.session() as session:
                run = RepositoryHub(session, runtime.artifacts).get_run(user_id, run_id)
                if not run.final_paper_pdf_path:
                    raise FileNotFoundError("最终 PDF 尚未生成")
                path = runtime.artifacts.resolve(run.final_paper_pdf_path)
            return FileResponse(path, media_type="application/pdf", filename=f"{run_id}-paper.pdf")
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.get("/api/runs/{run_id}/final-paper/source")
    async def download_final_paper_source(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> FileResponse:
        """下载当前用户最终单文件 TeX 源码。"""
        try:
            with runtime.database.session() as session:
                run = RepositoryHub(session, runtime.artifacts).get_run(user_id, run_id)
                if not run.final_paper_tex_path:
                    raise FileNotFoundError("最终 TeX 源码尚未生成")
                path = runtime.artifacts.resolve(run.final_paper_tex_path)
            return FileResponse(path, media_type="text/x-tex; charset=utf-8", filename=f"{run_id}-paper.tex")
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    @app.get("/api/runs/{run_id}/legacy-final-report")
    async def legacy_final_report(
        run_id: str,
        user_id: str = Depends(current_user_id),
    ) -> FileResponse:
        """只为历史 Run 下载旧版 Markdown，不把它伪装成 PDF。"""
        try:
            with runtime.database.session() as session:
                run = RepositoryHub(session, runtime.artifacts).get_run(user_id, run_id)
                if not run.final_report_path:
                    raise FileNotFoundError("该 Run 没有旧版 Markdown 论文")
                path = runtime.artifacts.resolve(run.final_report_path)
                if path.suffix.lower() != ".md":
                    raise FileNotFoundError("该 Run 没有旧版 Markdown 论文")
            return FileResponse(
                path,
                media_type="text/markdown; charset=utf-8",
                filename=f"{run_id}-legacy-paper.md",
            )
        except Exception as exc:
            raise api_error(exc, default_status=404) from exc

    return app


def _user_payload(item: Any, *, groups: list[Any] | None = None) -> dict[str, Any]:
    return {
        "user_id": item.user_id,
        "email": item.email,
        "display_name": item.display_name,
        "institution": item.institution or "",
        "department": item.department or "",
        "position": item.position or "",
        "role": item.role,
        "is_developer": bool(getattr(item, "is_developer", False)),
        "status": item.status,
        "storage_key": getattr(item, "storage_key", None),
        "groups": [group.name for group in (groups or [])],
        "default_model_strategy": item.default_model_strategy,
        "default_budget_preset": item.default_budget_preset,
        "website_language": getattr(item, "website_language", "zh") or "zh",
        "default_contract_language": getattr(item, "default_contract_language", "zh") or "zh",
        "default_research_language": getattr(item, "default_research_language", "zh") or "zh",
        "contract_chat_panel_width": float(getattr(item, "contract_chat_panel_width", 0.34) or 0.34),
        "email_verified_at": utc_iso(item.email_verified_at),
        "credit_balance": float(item.credit_balance or 0.0),
        "monthly_cost_limit": float(item.monthly_cost_limit) if item.monthly_cost_limit is not None else None,
        "max_concurrent_research_runs": int(item.max_concurrent_research_runs or 1),
        "storage_quota_bytes": int(item.storage_quota_bytes) if getattr(item, "storage_quota_bytes", None) is not None else None,
        "created_at": utc_iso(item.created_at),
    }


def _group_payload(
    item: Any,
    *,
    members: list[dict[str, Any]],
    membership_mode: str = "manual",
) -> dict[str, Any]:
    return {
        "group_id": item.group_id,
        "system_key": getattr(item, "system_key", None),
        "name": item.name,
        "description": item.description,
        "status": item.status,
        "default_monthly_cost_limit": item.default_monthly_cost_limit,
        "default_max_concurrent_runs": item.default_max_concurrent_runs,
        "default_model_strategy": item.default_model_strategy,
        "default_budget_preset": item.default_budget_preset,
        "members": members,
        "member_count": len(members),
        "membership_mode": membership_mode,
        "created_at": utc_iso(item.created_at),
    }


def _project_payload(item: Any, deletion_job: Any | None = None) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "name": item.name,
        "description": item.description,
        "sort_order": item.sort_order,
        "created_at": utc_iso(item.created_at),
        "updated_at": utc_iso(item.updated_at),
        "deletion_job": _deletion_job_payload(deletion_job) if deletion_job is not None else None,
    }


def _conversation_list_payload(
    item: Any,
    deletion_job: Any | None = None,
    *,
    runtime_phase: str | None = None,
) -> dict[str, Any]:
    run = item.research_run
    return {
        "conversation_id": item.conversation_id,
        "project_id": item.project_id,
        "sort_order": item.sort_order,
        "display_name": item.display_name,
        "status": item.status,
        "latest_contract_version": item.latest_contract_version,
        "contract_language": getattr(item, "contract_language", "zh") or "zh",
        "completed_run_id": item.completed_run_id,
        "run_id": run.run_id if run is not None else None,
        "run_status": run.status if run is not None else None,
        "runtime_phase": runtime_phase,
        "created_at": utc_iso(item.created_at),
        "updated_at": utc_iso(item.updated_at),
        "deletion_job": _deletion_job_payload(deletion_job) if deletion_job is not None else None,
    }


def _recent_research_payload(
    repo: RepositoryHub,
    conversations: list[Any],
    run_events_by_id: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """生成首页最近研究摘要；按 Conversation/Run 活动时间选取六项并只读取公开进展。"""
    selected = sorted(conversations, key=_conversation_activity_timestamp, reverse=True)[:6]
    result: list[dict[str, Any]] = []
    for conversation in selected:
        run = conversation.research_run
        run_events = (
            (run_events_by_id or {}).get(run.run_id, [])
            if run is not None
            else []
        )
        if run is not None and run_events_by_id is None:
            run_events = repo.read_events("run", run.run_id)
        public_events = repo.read_events("public_run", run.run_id) if run is not None else []
        latest_public = public_events[-1] if public_events else None
        activity_at = _conversation_activity_at(conversation)
        if latest_public is not None and latest_public.created_at.timestamp() > activity_at.timestamp():
            activity_at = latest_public.created_at
        result.append(
            {
                "conversation_id": conversation.conversation_id,
                "project_id": conversation.project_id,
                "display_name": conversation.display_name,
                "conversation_status": conversation.status,
                "latest_contract_version": conversation.latest_contract_version,
                "run_id": run.run_id if run is not None else None,
                "run_status": run.status if run is not None else None,
                "runtime_phase": _home_runtime_phase(run, run_events),
                "latest_progress": (
                    {
                        "title": str((latest_public.payload or {}).get("title") or "研究进展"),
                        "summary": latest_public.message,
                        "created_at": utc_iso(latest_public.created_at),
                    }
                    if latest_public is not None
                    else None
                ),
                "updated_at": utc_iso(activity_at),
            }
        )
    return result


def _active_run_events(
    repo: RepositoryHub, conversations: list[Any]
) -> dict[str, list[Any]]:
    """只读取需要用论文事件细分阶段的 active Run，供首页和左栏共用。"""
    return {
        run.run_id: repo.read_events("run", run.run_id)
        for conversation in conversations
        if (run := conversation.research_run) is not None
        and run.status in {"running", "draining"}
    }


def _conversation_runtime_phase(
    conversation: Any, run_events_by_id: dict[str, list[Any]]
) -> str | None:
    """从 Conversation 的 Run 与当前 Attempt 事件推导用户可见阶段。"""
    run = conversation.research_run
    return _home_runtime_phase(
        run,
        run_events_by_id.get(run.run_id, []) if run is not None else [],
    )


def _conversation_activity_at(conversation: Any):
    """以 Conversation 更新时间和 Run 心跳/终态时间中的最新值表示首页活动时间。"""
    candidates = [conversation.updated_at]
    run = conversation.research_run
    if run is not None:
        candidates.extend(
            value
            for value in (
                run.last_heartbeat_at,
                run.completed_at,
                run.started_at,
                run.created_at,
            )
            if value is not None
        )
    return max(candidates, key=lambda value: value.timestamp())


def _conversation_activity_timestamp(conversation: Any) -> float:
    return _conversation_activity_at(conversation).timestamp()


def _home_runtime_phase(run: Any | None, run_events: list[Any]) -> str | None:
    """把 Run 状态和论文阶段事件压缩为首页使用的稳定阶段，不暴露内部事件名。"""
    if run is None:
        return None
    if run.status in {"completed", "failed", "interrupted"}:
        return run.status
    if run.status == "paused":
        return "paused"
    if run.status == "resuming":
        return "resuming"
    latest_attempt = max(run.attempts, key=lambda item: item.attempt_number, default=None)
    attempt_started = latest_attempt.started_at if latest_attempt else None
    if attempt_started is not None and attempt_started.tzinfo is None:
        attempt_started = attempt_started.replace(tzinfo=timezone.utc)
    relevant = []
    for item in run_events:
        created = item.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if attempt_started is None or created >= attempt_started:
            relevant.append(item)
    last_reopened = max(
        (item.sequence for item in relevant if item.kind == "research_reopened"),
        default=0,
    )
    if last_reopened:
        relevant = [item for item in relevant if item.sequence > last_reopened]
    kinds = {item.kind for item in relevant}
    if "paper_published" in kinds:
        return "paper_published"
    if "paper_revision_started" in kinds:
        return "paper_revision"
    if "paper_audit_started" in kinds:
        return "paper_audit"
    if "paper_writing_started" in kinds or "run_final_state_reconciled" in kinds:
        return "paper_writing"
    if "closure_drain_completed" in kinds:
        return "draining"
    if run.status == "draining":
        return "draining"
    if run.status in {"created", "queued"}:
        return "waiting"
    return "research"


def _deletion_job_payload(item: Any) -> dict[str, Any]:
    return {
        "deletion_job_id": item.deletion_job_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "target_name": item.target_name,
        "status": item.status,
        "error": item.error,
        "created_at": utc_iso(item.created_at),
        "updated_at": utc_iso(item.updated_at),
        "completed_at": utc_iso(item.completed_at),
    }


def _file_payload(item: Any) -> dict[str, Any]:
    return {
        "file_id": item.file_id,
        "name": item.original_name,
        "extension": item.extension,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "created_at": utc_iso(item.created_at),
    }


def _generated_payload(item: Any) -> dict[str, Any]:
    return {
        "conversation_id": item.conversation_id,
        "version": item.version,
        "contract_version_id": item.contract_version_id,
        "markdown": item.markdown,
        "warnings": item.warnings,
        "execution_policy": dict(item.execution_policy),
    }


def _public_run_event_payload(item: Any) -> dict[str, Any]:
    """把 public_run 记录转换成稳定、无内部事件名的浏览器 DTO。"""
    payload = dict(item.payload or {})
    return {
        "sequence": item.sequence,
        "category": item.kind,
        "title": str(payload.get("title") or "研究进展"),
        "summary": item.message,
        "node_id": payload.get("node_id"),
        "tone": str(payload.get("tone") or "research"),
        "created_at": utc_iso(item.created_at),
    }


def _legacy_public_summary(message: str) -> str:
    """为历史 Run 生成一次性短投影，避免旧长报告重新污染页面。"""
    plain = re.sub(r"[`*_>#]", "", str(message or ""))
    plain = " ".join(plain.split())
    if len(plain) <= 237:
        return plain
    return plain[:237].rstrip() + "…"


def _conversation_detail(runtime: VNextApplication, user_id: str, conversation_id: str) -> dict[str, Any]:
    with runtime.database.session() as session:
        repo = RepositoryHub(session, runtime.artifacts)
        item = repo.get_conversation(user_id, conversation_id)
        versions: list[dict[str, Any]] = []
        for version in item.versions:
            markdown = runtime.artifacts.read_text(version.markdown_path)
            document = parse_contract(markdown)
            validation = validate_contract(document)
            versions.append(
                {
                    "contract_version_id": version.contract_version_id,
                    "version": version.version,
                    "title": version.title,
                    "markdown": markdown,
                    "sections": version.sections,
                    "source_manifest": version.source_manifest,
                    "contract_language": getattr(version, "contract_language", "zh") or "zh",
                    "created_by": version.created_by,
                    "created_at": utc_iso(version.created_at),
                    "validation": {"errors": validation.errors, "warnings": validation.warnings},
                }
            )
        draft = None
        if item.working_draft is not None:
            draft_doc = parse_contract(render_contract(item.working_draft.title, item.working_draft.sections))
            validation = validate_contract(draft_doc)
            draft = {
                "base_version": item.working_draft.base_version,
                "title": item.working_draft.title,
                "sections": dict(item.working_draft.sections),
                "source_manifest": list(item.working_draft.source_manifest),
                "contract_language": getattr(item.working_draft, "contract_language", "zh") or "zh",
                "draft_revision": int(item.working_draft.draft_revision),
                "updated_at": utc_iso(item.working_draft.updated_at),
                "validation": {"errors": validation.errors, "warnings": validation.warnings},
            }
        run = None
        if item.research_run is not None:
            run = {
                "run_id": item.research_run.run_id,
                "status": item.research_run.status,
                "completion_kind": item.research_run.completion_kind,
                "execution_policy": dict(item.research_run.execution_policy or {}),
                "research_language": getattr(item.research_run, "research_language", "zh") or "zh",
            }
        latest_generation = repo.latest_generation_attempt(user_id, conversation_id)
        generation = None
        if latest_generation is not None:
            generation_pending = latest_generation.status in {
                "queued", "running", "repairing", "interrupted"
            }
            stages = repo.ensure_generation_stages(
                latest_generation.generation_attempt_id
            )
            generation = {
                "generation_attempt_id": latest_generation.generation_attempt_id,
                "attempt_number": latest_generation.attempt_number,
                "kind": latest_generation.kind,
                "status": latest_generation.status,
                "contractor_profile": latest_generation.contractor_profile,
                "resulting_contract_version_id": latest_generation.resulting_contract_version_id,
                "target_version": (
                    int(item.latest_contract_version) + 1 if generation_pending else None
                ),
                "resumable": latest_generation.status == "interrupted",
                "phases": runtime.contracts._generation_phase_payload(stages),
                "error": latest_generation.error,
                "elapsed_seconds": runtime.contracts.generation_elapsed_seconds(
                    latest_generation
                ),
                "created_at": utc_iso(latest_generation.created_at),
                "started_at": utc_iso(latest_generation.started_at),
                "completed_at": utc_iso(latest_generation.completed_at),
            }
        request_draft = None
        if item.status == "request_draft":
            request_draft = {
                "research_problem": item.research_problem,
                "background_and_prior_progress": item.background_and_prior_progress,
                "method_preferences_and_restrictions": item.method_preferences_and_restrictions,
                "valuable_partial_outcomes": item.valuable_partial_outcomes,
                "possible_ideas_and_rough_approaches": item.possible_ideas_and_rough_approaches,
                "contractor_profile": item.contractor_profile,
                "contract_language": getattr(item, "contract_language", "zh") or "zh",
                "source_file_ids": list(item.source_file_ids),
                "draft_revision": int(item.request_draft_revision),
            }
        request_snapshot = {
            "research_problem": item.research_problem,
            "background_and_prior_progress": item.background_and_prior_progress,
            "method_preferences_and_restrictions": item.method_preferences_and_restrictions,
            "valuable_partial_outcomes": item.valuable_partial_outcomes,
            "possible_ideas_and_rough_approaches": item.possible_ideas_and_rough_approaches,
            "contractor_profile": item.contractor_profile,
            "contract_language": getattr(item, "contract_language", "zh") or "zh",
            "source_file_ids": list(item.source_file_ids),
            "source_manifest": repo.source_manifest(user_id, item.source_file_ids),
        }
        return {
            **_conversation_list_payload(item),
            "contractor_profile": item.contractor_profile,
            "execution_policy": dict(item.execution_policy or {}),
            "contract_language": getattr(item, "contract_language", "zh") or "zh",
            "request_draft": request_draft,
            "request_snapshot": request_snapshot,
            "versions": versions,
            "working_draft": draft,
            "contract_chat_messages": [
                {
                    "chat_message_id": message.chat_message_id,
                    "contract_version_id": message.contract_version_id,
                    "role": message.role,
                    "content": message.content,
                    "sequence": message.sequence,
                    "draft_revision_before": message.draft_revision_before,
                    "draft_revision_after": message.draft_revision_after,
                    "created_at": utc_iso(message.created_at),
                }
                for message in repo.list_contract_chat_messages(user_id, conversation_id)
            ],
            "contract_chat_event_sequences": {
                version.contract_version_id: repo.latest_event_sequence(
                    "contract_chat", version.contract_version_id
                )
                for version in item.versions
            },
            "contract_chat_event_sequence": repo.latest_event_sequence(
                "contract_chat",
                next(
                    (
                        version.contract_version_id
                        for version in item.versions
                        if version.version == item.latest_contract_version
                    ),
                    "",
                ),
            ),
            "source_manifest": repo.source_manifest(user_id, item.source_file_ids),
            "generation": generation,
            "run": run,
            "editable_section_keys": list(EDITABLE_KEYS),
        }
