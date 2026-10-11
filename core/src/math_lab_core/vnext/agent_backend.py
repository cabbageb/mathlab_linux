from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Protocol
from contextlib import AsyncExitStack
import asyncio
import hashlib
import json
import shutil
import time

from math_planner.codex_exec import CodexExecClient as PlannerCodexClient
from math_planner.contract_app_server import ContractAppServerClient
from math_planner.config import AppConfig as PlannerConfig, ModelConfig as PlannerModel
from math_prover.codex_exec import CodexExecClient as ProverCodexClient
from math_prover.codex_permissions import managed_runtime_workspace
from math_prover.config import AppConfig as ProverConfig, ModelConfig as ProverModel
from math_prover.latex_publication import FIXED_LATEX_PREAMBLE
from math_prover.literature_access import (
    LiteratureAccessServer,
    LiteratureLimits,
    run_literature_preflight,
)
from math_prover.reviewer_evidence_access import (
    ReviewerEvidenceAccessServer,
    read_reviewer_evidence_audit,
)
from .languages import output_language_instruction


def prover_model_profiles(config: ProverConfig) -> dict[str, dict[str, dict[str, str]]]:
    """把已核验的 Prover profile 转为可冻结到 JSON execution policy 的角色映射。"""
    return {
        profile: {
            role: {
                "model": model.model,
                "reasoning_effort": model.reasoning_effort,
            }
            for role, model in table.items()
        }
        for profile, table in config.model_profiles.items()
    }


@dataclass(frozen=True)
class AgentTurn:
    text: str
    status: str
    thread_id: str | None
    usage: dict[str, int]
    model: str
    reasoning_effort: str
    error: str | None = None
    evidence: dict[str, object] | None = None
    usage_semantics: str = "per_turn"


class AgentBackend(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ContractCodexBackend:
    """复用现有 Planner Codex transport，但使用 vNext Contract 角色与工作区。"""

    def __init__(self, config: PlannerConfig, prompts_root: Path, runtime_root: Path) -> None:
        self.base_config = config
        self.config = replace(config, runtime_dir=runtime_root.resolve())
        self.prompts_root = prompts_root
        self.runtime_root = runtime_root.resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.client = PlannerCodexClient(self.config)
        self.chat_client = ContractAppServerClient(self.config)
        self._clients: dict[Path, PlannerCodexClient] = {}
        self._chat_clients: dict[Path, ContractAppServerClient] = {}

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> None:
        for client in self._chat_clients.values():
            await client.stop()
        for client in self._clients.values():
            await client.stop()
        self._chat_clients.clear(); self._clients.clear()
        await self.chat_client.stop()
        await self.client.stop()

    async def _scope_clients(self, runtime_root: Path | None) -> tuple[PlannerCodexClient, ContractAppServerClient, Path]:
        if runtime_root is None:
            return self.client, self.chat_client, self.runtime_root
        root = runtime_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        client = self._clients.get(root)
        chat = self._chat_clients.get(root)
        if client is None:
            scoped = replace(self.base_config, runtime_dir=root)
            client = PlannerCodexClient(scoped); self._clients[root] = client
            await client.start()
        if chat is None:
            scoped = replace(self.base_config, runtime_dir=root)
            chat = ContractAppServerClient(scoped); self._chat_clients[root] = chat
        return client, chat, root

    async def run_role(
        self,
        *,
        role: str,
        scope_id: str,
        task: str,
        profile: str,
        persistent: bool,
        provider_thread_id: str | None,
        repair_number: int = 0,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thread_ready: Callable[[str], Awaitable[None]] | None = None,
        runtime_root: Path | None = None,
        output_language: str = "zh",
    ) -> AgentTurn:
        client, chat_client, scoped_root = await self._scope_clients(runtime_root)
        model = self._model(role, profile)
        role_prompt = self.prompts_root / f"{role}.md"
        if not role_prompt.is_file():
            raise FileNotFoundError(f"Contract role prompt 不存在：{role_prompt}")
        common = (self.prompts_root / "common.md").read_text(encoding="utf-8")
        language_rule = output_language_instruction(output_language, domain="contract")
        instructions = (
            common.rstrip()
            + "\n\n"
            + role_prompt.read_text(encoding="utf-8").rstrip()
            + ("\n\n" + language_rule.rstrip() if language_rule else "")
            + "\n"
        )
        workspace = scoped_root / "agents" / scope_id / role
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".math_planner_root").touch(exist_ok=True)
        if role == "contract_chat_assistant" and on_text_delta is not None:
            if provider_thread_id:
                await chat_client.resume_thread(
                    provider_thread_id,
                    model=model,
                    cwd=workspace,
                    developer_instructions=instructions,
                )
                logical = provider_thread_id
            else:
                logical = await chat_client.start_thread(
                    model=model,
                    cwd=workspace,
                    developer_instructions=instructions,
                    ephemeral=not persistent,
                )
            if on_thread_ready is not None:
                await on_thread_ready(logical)
            result = await chat_client.run_turn(
                thread_id=logical,
                text=task,
                model=model,
                cwd=workspace,
                on_text_delta=on_text_delta,
            )
            return AgentTurn(
                text=result.text,
                status=result.status,
                thread_id=result.thread_id,
                usage=result.usage,
                model=model.model,
                reasoning_effort=model.reasoning_effort,
                error=result.error,
                usage_semantics="per_turn",
            )
        if provider_thread_id:
            await client.resume_thread(
                provider_thread_id,
                model=model,
                cwd=workspace,
                developer_instructions=instructions,
                sandbox="workspace-write",
            )
            logical = provider_thread_id
        else:
            logical = await client.start_thread(
                model=model,
                cwd=workspace,
                developer_instructions=instructions,
                sandbox="workspace-write",
                ephemeral=not persistent,
            )
        if on_thread_ready is not None and provider_thread_id:
            await on_thread_ready(logical)
        result = await client.run_turn(
            thread_id=logical,
            text=task,
            model=model,
            cwd=workspace,
            record_dir=workspace / "turns" / f"repair-{repair_number:02d}",
            on_thread_started=on_thread_ready,
        )
        return AgentTurn(
            text=result.text,
            status=result.status,
            thread_id=result.thread_id or client.resolved_thread_id(logical),
            usage=result.usage,
            model=model.model,
            reasoning_effort=model.reasoning_effort,
            error=str(result.error) if result.error else None,
            usage_semantics="thread_cumulative",
        )

    def _model(self, role: str, profile: str) -> PlannerModel:
        mapping = {
            "contractor": "contractor",
            "contract_integrator": "contract_integrator",
            "contract_fidelity_auditor": "contract_fidelity_auditor",
            "contract_completeness_auditor": "contract_completeness_auditor",
            "contract_map_auditor": "contract_map_auditor",
            "contract_chat_assistant": "contractor",
        }
        key = mapping[role]
        profile_models = self.config.model_profiles.get(profile) or self.config.model_profiles["high"]
        return profile_models.get(key) or self.config.models[key]


class ProverCodexBackend:
    """面向动态 Research Graph 的角色执行后端；调度语义由 vNext Controller 管理。"""

    def __init__(
        self,
        config: ProverConfig,
        prompts_root: Path,
        runtime_root: Path,
        *,
        compute_timeout_seconds: int = 30,
    ) -> None:
        self.base_config = config
        self.config = replace(config, runtime_dir=runtime_root.resolve())
        self.prompts_root = prompts_root
        self.runtime_root = runtime_root.resolve()
        self.compute_timeout_seconds = max(1, int(compute_timeout_seconds))
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.client = ProverCodexClient(self.config)
        self._clients: dict[Path, ProverCodexClient] = {}
        self._workspace_locks: dict[Path, asyncio.Lock] = {}

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> None:
        for client in self._clients.values():
            await client.stop()
        self._clients.clear()
        await self.client.stop()

    async def _scope_client(self, runtime_root: Path | None) -> tuple[ProverCodexClient, Path]:
        if runtime_root is None:
            return self.client, self.runtime_root
        root = runtime_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        client = self._clients.get(root)
        if client is None:
            scoped = replace(self.base_config, runtime_dir=root)
            client = ProverCodexClient(scoped); self._clients[root] = client
            await client.start()
        return client, root

    async def preflight_literature(self) -> dict[str, object]:
        """在线程中执行不调用模型的受限检索预检，避免网络失效后仍开始付费 Run。"""
        return await asyncio.to_thread(run_literature_preflight, self.config)

    async def pause_run(self, runtime_root: Path, reason: str) -> None:
        """只暂停指定 Run 的 scoped Codex client，不影响其他并行 Run。"""
        root = runtime_root.expanduser().resolve()
        client = self._clients.get(root)
        if client is not None:
            await client.pause_active_turns(reason)

    async def resume_run(self, runtime_root: Path) -> None:
        """解除指定 Run 的 scoped transport barrier。"""
        root = runtime_root.expanduser().resolve()
        client = self._clients.get(root)
        if client is not None:
            client.resume_turns()

    def _agent_workspace(
        self, scoped_root: Path, scope_id: str, role: str
    ) -> Path:
        """用唯一规则解析角色 cwd，并保证它始终是 runtime 内的真实目录。"""
        scoped_config = replace(self.base_config, runtime_dir=scoped_root.resolve())
        return managed_runtime_workspace(
            scoped_config,
            scoped_root / "agents" / scope_id / role,
            "角色工作目录",
        )

    async def run_role(
        self,
        *,
        role: str,
        scope_id: str,
        task: str,
        persistent: bool,
        provider_thread_id: str | None,
        repair_number: int = 0,
        operation_id: str | None = None,
        network_access: bool = False,
        literature_access: bool = False,
        literature_limits: LiteratureLimits | None = None,
        literature_archive_root: Path | None = None,
        context_paths: list[Path] | None = None,
        reviewer_evidence_artifacts: list[dict[str, object]] | None = None,
        model_profile: dict[str, dict[str, str]] | None = None,
        runtime_root: Path | None = None,
        on_thread_ready: Callable[[str], Awaitable[None]] | None = None,
        writer_deadline_mode: str = "bounded",
        output_language: str = "zh",
    ) -> AgentTurn:
        scoped_root = (
            runtime_root.expanduser().resolve()
            if runtime_root is not None
            else self.runtime_root
        )
        workspace = self._agent_workspace(scoped_root, scope_id, role)
        lock = self._workspace_locks.setdefault(workspace, asyncio.Lock())
        async with lock:
            return await self._run_role_unlocked(
                role=role,
                scope_id=scope_id,
                task=task,
                persistent=persistent,
                provider_thread_id=provider_thread_id,
                repair_number=repair_number,
                operation_id=operation_id,
                network_access=network_access,
                literature_access=literature_access,
                literature_limits=literature_limits,
                literature_archive_root=literature_archive_root,
                context_paths=context_paths,
                reviewer_evidence_artifacts=reviewer_evidence_artifacts,
                model_profile=model_profile,
                runtime_root=runtime_root,
                on_thread_ready=on_thread_ready,
                writer_deadline_mode=writer_deadline_mode,
                output_language=output_language,
            )

    async def _run_role_unlocked(
        self,
        *,
        role: str,
        scope_id: str,
        task: str,
        persistent: bool,
        provider_thread_id: str | None,
        repair_number: int = 0,
        operation_id: str | None = None,
        network_access: bool = False,
        literature_access: bool = False,
        literature_limits: LiteratureLimits | None = None,
        literature_archive_root: Path | None = None,
        context_paths: list[Path] | None = None,
        reviewer_evidence_artifacts: list[dict[str, object]] | None = None,
        model_profile: dict[str, dict[str, str]] | None = None,
        runtime_root: Path | None = None,
        on_thread_ready: Callable[[str], Awaitable[None]] | None = None,
        writer_deadline_mode: str = "bounded",
        output_language: str = "zh",
    ) -> AgentTurn:
        client, scoped_root = await self._scope_client(runtime_root)
        literature_roles = {
            "literature_worker",
            "adversarial_prior_art_scout",
            "related_work_context_scout",
        }
        if literature_access and role not in literature_roles:
            raise ValueError("Controller 只允许研究文献 Worker 与 publication Scouts 获得文献访问能力")
        if literature_access and network_access:
            raise ValueError("文献 Worker 只能通过 Controller 回环服务访问外网")
        if reviewer_evidence_artifacts is not None and role != "architecture_reviewer":
            raise ValueError("Controller 只允许 Architecture Reviewer 使用全局证据浏览器")
        model = self._model(role, model_profile)
        prompt_path = self._prompt_path(role)
        role_prompt = prompt_path.read_text(encoding="utf-8")
        marker = "enabled" if network_access else "disabled"
        workspace = self._agent_workspace(scoped_root, scope_id, role)
        context_dir = workspace / "input" / "source_materials"
        if context_dir.exists():
            shutil.rmtree(context_dir)
        if context_paths:
            context_dir.mkdir(parents=True, exist_ok=True)
            if role in {"paper_writer", "paper_auditor"}:
                paper_dir = context_dir / "paper_sources"
                if paper_dir.exists():
                    shutil.rmtree(paper_dir)
                paper_dir.mkdir(parents=True, exist_ok=True)
                for source in context_paths:
                    source = Path(source)
                    if source.is_dir():
                        for item in source.iterdir():
                            target = paper_dir / item.name
                            if item.is_dir():
                                shutil.copytree(item, target)
                            elif item.is_file():
                                shutil.copy2(item, target)
                    elif source.is_file():
                        shutil.copy2(source, paper_dir / source.name)
            elif role in {"adversarial_prior_art_scout", "related_work_context_scout"}:
                if len(context_paths) != 1 or not Path(context_paths[0]).is_file():
                    raise ValueError("Publication Scout 必须且只能接收一份 paper-v1.tex")
                shutil.copy2(Path(context_paths[0]), context_dir / "paper-v1.tex")
            else:
                for index, source in enumerate(context_paths, start=1):
                    source = Path(source)
                    if source.is_file():
                        target = context_dir / f"{index:03d}-{source.name}"
                        if not target.exists() or target.stat().st_mtime_ns != source.stat().st_mtime_ns:
                            shutil.copy2(source, target)
        marker_path = workspace / ".math_prover_root"
        marker_path.touch(exist_ok=True)
        operation_key = operation_id or "legacy-operation"
        operation_root = workspace / "operations" / operation_key
        operation_root.mkdir(parents=True, exist_ok=True)
        literature_guide = ""
        reviewer_guide = ""
        reviewer_audit_path = operation_root / "controller" / "reviewer_evidence_audit.jsonl"
        async with AsyncExitStack() as stack:
            if literature_access:
                access = await stack.enter_async_context(
                    LiteratureAccessServer(
                        self.config,
                        operation_root,
                        cache_root=operation_root / "controller" / "download-cache",
                        limits=literature_limits,
                        archive_root=literature_archive_root,
                    )
                )
                literature_guide = self._install_literature_tools(
                    workspace, access.client_payload()
                )
            if reviewer_evidence_artifacts is not None:
                reviewer_access = await stack.enter_async_context(
                    ReviewerEvidenceAccessServer(
                        [dict(item) for item in reviewer_evidence_artifacts],
                        reviewer_audit_path,
                    )
                )
                reviewer_guide = self._install_reviewer_evidence_tool(
                    workspace, reviewer_access.client_payload()
                )
            instructions = self._compose_instructions(
                role=role,
                role_prompt=role_prompt,
                network_marker=marker,
                workspace=workspace,
                literature_guide=literature_guide,
                reviewer_guide=reviewer_guide,
                output_language=output_language,
            )
            if provider_thread_id:
                await client.resume_thread(
                    provider_thread_id,
                    model=model,
                    cwd=workspace,
                    instructions=instructions,
                )
                logical = provider_thread_id
            else:
                logical = await client.start_thread(
                    model=model,
                    cwd=workspace,
                    instructions=instructions,
                    persistent=persistent,
                )
            turn_started_ns = time.time_ns()
            record_dir = (
                workspace / "turns" / operation_key / f"repair-{repair_number:02d}"
            )
            deadline_warning_sent = False
            if role == "paper_writer" and writer_deadline_mode == "bounded":
                writer_timeout = max(1, int(self.config.paper_writer_timeout_seconds))
                warning_remaining = min(
                    max(1, int(self.config.paper_writer_warning_remaining_seconds)),
                    max(1, writer_timeout - 1),
                )
                first_window = max(1, writer_timeout - warning_remaining)
                result = await client.run_turn(
                    thread_id=logical,
                    text=task,
                    model=model,
                    record_dir=record_dir,
                    on_thread_ready=on_thread_ready,
                    timeout_seconds=first_window,
                    timeout_status="deadline_warning",
                )
                if result.status == "deadline_warning":
                    deadline_warning_sent = True
                    logical = result.thread_id or logical
                    await client.resume_thread(
                        logical,
                        model=model,
                        cwd=workspace,
                        instructions=instructions,
                    )
                    result = await client.run_turn(
                        thread_id=logical,
                        text=(
                            "你只剩十分钟了，请尽快完成论文写作与编译。"
                            "立即停止扩展、反复重读和非必要检查；"
                            "优先将当前完整稿写入 paper.tex 并提交完整 LaTeX。"
                            "Controller 会负责最终编译门禁。"
                        ),
                        model=model,
                        record_dir=record_dir / "deadline-warning",
                        timeout_seconds=warning_remaining,
                        timeout_status="deadline_expired",
                    )
            else:
                result = await client.run_turn(
                    thread_id=logical,
                    text=task,
                    model=model,
                    record_dir=record_dir,
                    on_thread_ready=on_thread_ready,
                    timeout_seconds=(0 if role == "paper_writer" else None),
                )
        submitted_text = result.text
        submitted_candidates = {
            "paper_writer": (workspace / "paper.tex",),
            "paper_auditor": (
                workspace / "audit.md",
                workspace / "paper_audit.md",
            ),
            "adversarial_prior_art_scout": (
                workspace / "report.md",
                workspace / "prior_art_report.md",
            ),
            "related_work_context_scout": (
                workspace / "report.md",
                workspace / "related_work_report.md",
            ),
        }.get(role, ())
        eligible = [
            path
            for path in submitted_candidates
            if path.is_file() and path.stat().st_mtime_ns >= turn_started_ns
        ]
        submitted_file = max(eligible, key=lambda path: path.stat().st_mtime_ns) if eligible else None
        if submitted_file is not None:
            submitted_text = submitted_file.read_text(encoding="utf-8")
        evidence = self._collect_literature_evidence(operation_root) if literature_access else None
        if role == "paper_writer" and (deadline_warning_sent or result.status == "deadline_expired"):
            evidence = {
                "kind": "paper_writer_deadline",
                "deadline_warning_sent": deadline_warning_sent,
                "deadline_expired": result.status == "deadline_expired",
                "writer_timeout_seconds": int(self.config.paper_writer_timeout_seconds),
            }
        if reviewer_evidence_artifacts is not None:
            evidence = {
                "kind": "reviewer_evidence",
                **read_reviewer_evidence_audit(reviewer_audit_path),
            }
        if evidence is not None and submitted_file is not None and submitted_file.is_file():
            evidence["submitted_artifact_path"] = str(submitted_file)
            evidence["submitted_artifact_sha256"] = hashlib.sha256(
                submitted_file.read_bytes()
            ).hexdigest()
        return AgentTurn(
            text=submitted_text,
            status=result.status,
            thread_id=result.thread_id or client.resolved_thread_id(logical),
            usage=result.usage,
            model=model.model,
            reasoning_effort=model.reasoning_effort,
            error=result.error,
            evidence=evidence,
            usage_semantics="thread_cumulative",
        )

    def _compose_instructions(
        self,
        *,
        role: str,
        role_prompt: str,
        network_marker: str,
        workspace: Path,
        literature_guide: str = "",
        reviewer_guide: str = "",
        output_language: str = "zh",
    ) -> str:
        """为出版角色隔离研究图语言，其余角色继续组合公共契约与受限工具策略。"""
        shell_rule = (
            "Shell 工具调用必须省略 `workdir` 参数；"
            "Controller 已将当前 Agent 工作区固定为会话 cwd。"
        )
        language_rule = output_language_instruction(output_language, domain="research")
        if role in {
            "paper_writer",
            "paper_auditor",
            "adversarial_prior_art_scout",
            "related_work_context_scout",
        }:
            return (
                f"<!-- controller-network-access: {network_marker} -->\n"
                "<!-- standalone-publication-role: true -->\n\n"
                + shell_rule
                + "\n\n"
                + role_prompt.rstrip()
                + ("\n\n" + literature_guide.rstrip() if literature_guide else "")
                + ("\n\n" + language_rule.rstrip() if language_rule else "")
                + "\n"
            )
        if role in {"public_summarizer", "node_public_summarizer"}:
            return (
                f"<!-- controller-network-access: {network_marker} -->\n"
                "<!-- standalone-public-summary-role: true -->\n\n"
                + shell_rule
                + "\n\n"
                + role_prompt.rstrip()
                + ("\n\n" + language_rule.rstrip() if language_rule else "")
                + "\n"
            )
        common = (self.prompts_root / "common/role_contract.md").read_text(encoding="utf-8")
        compute_guide = self._install_compute_policy(workspace)
        return (
            f"<!-- controller-network-access: {network_marker} -->\n"
            f"<!-- controller-compute-timeout-seconds: {self.compute_timeout_seconds} -->\n\n"
            + shell_rule
            + "\n\n"
            + common.rstrip()
            + "\n\n"
            + role_prompt.rstrip()
            + "\n\n"
            + compute_guide.rstrip()
            + ("\n\n" + literature_guide.rstrip() if literature_guide else "")
            + ("\n\n" + reviewer_guide.rstrip() if reviewer_guide else "")
            + ("\n\n" + language_rule.rstrip() if language_rule else "")
            + "\n"
        )

    def _install_reviewer_evidence_tool(
        self,
        workspace: Path,
        access_payload: dict[str, str],
    ) -> str:
        """安装当前 Review 专属的白名单证据 list/open 工具。"""
        tools_dir = workspace / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        project_root = self.prompts_root.parents[2]
        source = project_root / "scripts" / "tools" / "reviewer_evidence.py"
        if not source.is_file():
            raise FileNotFoundError(f"Reviewer evidence 工具不存在：{source}")
        shutil.copy2(source, tools_dir / source.name)
        input_dir = workspace / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        (input_dir / "reviewer_evidence_access.json").write_text(
            json.dumps(access_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return """# Controller-provided Reviewer evidence browser

默认上下文只包含压缩白板。需要核实局部数学细节时，只能使用：

1. `python3 tools/reviewer_evidence.py list [--kind KIND]`
2. `python3 tools/reviewer_evidence.py open --artifact-id '<ID>'`

该工具只读取当前 Run 的冻结白名单，并记录本轮实际打开的 Artifact。Private notes 只用于导航，不能替代数学证据。
"""


    def _install_compute_policy(self, workspace: Path) -> str:
        """Install the bounded sanity-check helper in every Prover workspace.

        The model still has ordinary file/shell tools for document handling.  Any
        Python/symbolic computation requested by the role contract must go through
        this helper, which enforces the configured wall-clock cap.
        """
        tools_dir = workspace / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        project_root = self.prompts_root.parents[2]
        source = project_root / "scripts" / "tools" / "bounded_compute.py"
        target = tools_dir / "bounded_compute.py"
        if source.is_file():
            shutil.copy2(source, target)
        guide = (
            "# Controller-provided bounded computation policy\n\n"
            "The deployment may have very limited CPU and memory.  File reading, "
            "theorem search and ordinary text processing remain available.  If you "
            "need an exact/symbolic/toy computation, invoke it only through:\n\n"
            f"`python3 tools/bounded_compute.py --timeout {self.compute_timeout_seconds} -- <command ...>`\n\n"
            "The managed shell provides `python3`, NumPy and SciPy.  NumPy/SciPy may "
            "be used only for small deterministic linear-algebra or optimization sanity "
            "checks; floating-point output is exploratory evidence and never replaces "
            "a proof or an exact certificate.  Use `fractions`, `decimal` and small "
            "explicit exact linear-algebra routines when the claimed conclusion needs "
            "exact verification.  Other optional packages are not guaranteed and must "
            "not be probed for.  Save scratch scripts in the current "
            "Agent workspace and invoke them by relative path.  Do not use `/tmp` or "
            "`/var/tmp`: temporary paths are isolated between shell calls.\n\n"
            "Do not launch simulations, random searches, training, "
            "parameter sweeps or any unbounded computation.  For finite enumeration, "
            "the first probe must use at most 5,000 objects/indices and should target "
            "at most 5 seconds.  Scale up only after measuring that probe and only when "
            "the projected run stays below 20 seconds.  The helper always reports "
            "`bounded_compute_status`; only `ok` is a successful sanity check.  After "
            "any `command_failed` or `resource_limit_exceeded`, do "
            "not repeat the same or a larger computation; at most one final retry at "
            "one tenth of the previous scale is allowed.  A timeout is a resource limit, "
            "not a mathematical result; otherwise continue by reasoning from the work "
            "already completed.  Finite checks never replace a proof.\n"
        )
        (workspace / "COMPUTE_POLICY.md").write_text(guide, encoding="utf-8")
        return guide

    def _install_literature_tools(
        self, workspace: Path, access_payload: dict[str, object]
    ) -> str:
        """安装四个 Controller 托管文献工具，并把一次性 token 限定在 Agent 私有输入。"""
        tools_dir = workspace / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        project_root = self.prompts_root.parents[2]
        for name in (
            "literature_tool_common.py",
            "theorem_search.py",
            "arxiv_search.py",
            "fetch_arxiv.py",
            "paper_reader.py",
        ):
            source = project_root / "scripts" / "tools" / name
            if not source.is_file():
                raise FileNotFoundError(f"文献工具不存在：{source}")
            shutil.copy2(source, tools_dir / name)
        input_dir = workspace / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        config_path = input_dir / "literature_access.json"
        config_path.write_text(
            json.dumps(access_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        limits = dict(access_payload.get("limits") or {})
        guide = f"""# Controller-provided literature tools

你不能直接访问外网；必须使用以下回环工具。它们由 Controller 使用部署环境中的标准 HTTP(S) 代理，且证据会自动冻结：

1. `python3 tools/theorem_search.py --query '<精确 theorem query>' --top-k 5`
2. `python3 tools/arxiv_search.py --query '<arXiv query>' --max-results 5`
3. 从摘要中选择一个带 `vN` 的明确版本后：`python3 tools/fetch_arxiv.py --arxiv-id '<id>vN'`
4. `python3 tools/paper_reader.py --paper-dir '<上一步 paper_root>' --query '<当前局部问题>'`

调用 `fetch_arxiv.py` 时，shell `timeout_ms` 必须至少为 180000；不要用 30 秒工具截止覆盖 Controller 的下载时限。

硬预算：theorem search {limits.get('theorem_searches')} 次，arXiv query {limits.get('arxiv_searches')} 次，最多查看 {limits.get('abstracts')} 篇摘要，只能物化 {limits.get('papers')} 篇论文。下载顺序固定为 source first、PDF second；若有 TeX，必须以 TeX 为主并用 PDF 提供页码锚点。网络/工具失败必须如实报告，不能伪装成“无匹配文献”。格式 repair 时复用已经冻结的材料，不得重新检索或下载。
"""
        (workspace / "LITERATURE_TOOLS.md").write_text(guide, encoding="utf-8")
        return guide

    @staticmethod
    def _collect_literature_evidence(operation_root: Path) -> dict[str, object]:
        """汇总工具证据；只公开 literature 业务材料和去敏网络审计，不公开 token。"""
        literature_root = operation_root / "literature"
        audit_path = operation_root / "controller" / "network_audit.jsonl"
        audit: list[dict[str, object]] = []
        if audit_path.is_file():
            audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        files: list[dict[str, object]] = []
        if literature_root.is_dir():
            for path in sorted(item for item in literature_root.rglob("*") if item.is_file() and item.name != "evidence_manifest.json"):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                files.append(
                    {
                        "path": path.relative_to(operation_root).as_posix(),
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                    }
                )
        metadata_files = sorted(literature_root.glob("papers/*/metadata.json")) if literature_root.is_dir() else []
        papers = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_files]
        summary = {
            "theorem_searches": sum(item.get("action") == "theorem_search" and bool(item.get("ok")) for item in audit),
            "arxiv_searches": sum(item.get("action") == "arxiv_search" and bool(item.get("ok")) for item in audit),
            "network_failures": sum(
                (not bool(item.get("ok"))) or bool(item.get("fallback_required"))
                for item in audit
            ),
            "papers": papers,
            "reading_evidence_count": len(list(literature_root.glob("papers/*/reading_evidence.json"))) if literature_root.is_dir() else 0,
        }
        manifest = {"summary": summary, "network_audit": audit, "files": files}
        literature_root.mkdir(parents=True, exist_ok=True)
        manifest_path = literature_root / "evidence_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "kind": "literature",
            "operation_root": str(operation_root),
            "manifest_path": str(manifest_path),
            **summary,
        }

    def _model(
        self,
        role: str,
        model_profile: dict[str, dict[str, str]] | None = None,
    ) -> ProverModel:
        """按当前 Run 冻结的 profile 选择角色模型；无 profile 仅供非 Run 工具兼容。"""
        mapping = {
            "literature_worker": "research_literature",
            "construction_worker": "research_construction",
            "general_proof_worker": "research_general_proof",
            "cross_domain_worker": "research_cross_domain",
            "node_supervisor": "research_node_supervisor",
            "architecture_reviewer": "research_architecture_reviewer",
            "claim_verifier": "research_claim_verifier",
            "paper_writer": "paper_writer",
            "paper_auditor": "paper_auditor",
            "adversarial_prior_art_scout": "paper_auditor",
            "related_work_context_scout": "paper_auditor",
            "public_summarizer": "public_summarizer",
            "node_public_summarizer": "public_summarizer",
        }
        model_key = mapping[role]
        if model_profile is None:
            return self.config.models[model_key]
        payload = model_profile.get(model_key)
        if payload is None:
            raise ValueError(f"Run execution model profile 缺少角色：{model_key}")
        return ProverModel(
            model=str(payload.get("model") or ""),
            reasoning_effort=str(payload.get("reasoning_effort") or ""),
        )

    def _prompt_path(self, role: str) -> Path:
        mapping = {
            "literature_worker": "workers/literature.md",
            "construction_worker": "workers/construction.md",
            "general_proof_worker": "workers/general_proof.md",
            "cross_domain_worker": "workers/cross_domain.md",
            "node_supervisor": "supervisor/node_supervisor.md",
            "architecture_reviewer": "architecture/reviewer.md",
            "claim_verifier": "verification/claim_verifier.md",
            "paper_writer": "report/paper_writer.md",
            "paper_auditor": "report/paper_auditor.md",
            "adversarial_prior_art_scout": "report/adversarial_prior_art_scout.md",
            "related_work_context_scout": "report/related_work_context_scout.md",
            "public_summarizer": "runtime/public_summarizer.md",
            "node_public_summarizer": "runtime/node_public_summarizer.md",
        }
        path = self.prompts_root / mapping[role]
        if not path.is_file():
            raise FileNotFoundError(f"vNext Prover prompt 不存在：{path}")
        return path




class OfflineContractBackend:
    """Hard offline backend. It never constructs or probes a live transport."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_role(self, **kwargs: object) -> AgentTurn:
        self.calls.append(dict(kwargs))
        role = str(kwargs.get("role") or "")
        if role == "contractor":
            task = str(kwargs.get("task") or "")
            text = (
                _OFFLINE_DIAGNOSIS
                if task.lstrip().startswith("现在执行 Contractor Stage 1")
                else _OFFLINE_CONTRACT
            )
        elif role == "contract_integrator":
            text = _OFFLINE_CONTRACT
        elif role == "contract_chat_assistant":
            text = _OFFLINE_CONTRACT_CHAT
        else:
            text = "# Contract Audit\n\n## Verdict\n\nPASS\n\n## Findings\n\n离线演示模式未进行真实数学审计。\n"
        on_text_delta = kwargs.get("on_text_delta")
        if callable(on_text_delta):
            for start in range(0, len(text), 24):
                await on_text_delta(text[start : start + 24])
        return AgentTurn(text, "completed", f"offline-{role}", {}, "offline-demo", "none")


class OfflineProverBackend:
    """Hard offline prover backend with deterministic, bounded role outputs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.supervisor_turns: dict[str, int] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_role(self, **kwargs: object) -> AgentTurn:
        self.calls.append(dict(kwargs))
        role = str(kwargs.get("role") or "")
        scope_id = str(kwargs.get("scope_id") or "offline")
        if role == "node_supervisor":
            count = self.supervisor_turns.get(scope_id, 0)
            self.supervisor_turns[scope_id] = count + 1
            text = _OFFLINE_SUPERVISOR if count == 0 else _OFFLINE_SUPERVISOR_REVIEW
        elif role.endswith("_worker"):
            text = _OFFLINE_WORKER
        elif role == "architecture_reviewer":
            text = _OFFLINE_ARCHITECTURE
        elif role == "claim_verifier":
            text = _OFFLINE_VERIFIER
        elif role == "paper_writer":
            text = _OFFLINE_REPORT
        elif role == "paper_auditor":
            text = _OFFLINE_REPORT_AUDIT
        elif role == "adversarial_prior_art_scout":
            text = _OFFLINE_PRIOR_ART_SCOUT
        elif role == "related_work_context_scout":
            text = _OFFLINE_CONTEXT_SCOUT
        elif role == "public_summarizer":
            text = "该研究阶段形成了新的数学判断，当前公开摘要由离线模式生成。"
        elif role == "node_public_summarizer":
            text = "# Research Node 展示\n\n## 标题\n\n离线研究节点\n\n## 当前研究问题\n\n离线模式只验证公开卡片的数据流。\n"
        else:
            raise KeyError(f"Unknown offline role: {role}")
        return AgentTurn(text, "completed", f"offline-{role}-{scope_id}", {}, "offline-demo", "none")


_OFFLINE_DIAGNOSIS = """# Effective Problem Brief and Mathematical Diagnosis

## Current Effective Problem

离线演示模式只验证两阶段生成链；真实数学问题保持为用户输入，不进行付费模型推理。

## Intent Resolution and Superseded Framing

当前没有可供离线模型判断的版本冲突；真实运行时 Contractor 会归并最新明确用户意图。

## Mathematical Objects, Quantifiers, and Ambiguities

数学对象、参数、量词与正则性均待 live Contractor 按原始 Research Request 正规化。

## Core Unknowns

需要识别原问题的主要真相分支、候选机制、必要条件与可能分类。

## Candidate Mechanisms and Formal Calculations

离线模式不生成数学候选，只保留产品演示结构。

## Minimal Sufficient Bridges

真实运行时应优先寻找完成原目标所需的最弱中间结论，避免规划过强 mega-theorem。

## Initial Research Portfolio

建议从一个 Discovery Node 恢复问题结构，再把精确候选正规化成 Claim。

## Structured and Directional Possibilities

候选出现后再研究唯一性、完整性和参数分类。
"""

_OFFLINE_CONTRACT = """# Research Contract

## A. Mathematical Contract

### Research Objective

在离线演示模式中保留用户原始 Research Request；未进行真实数学正规化。

### Mathematical Objects and Assumptions

以用户输入为准，待真实 Contractor 核验对象、量词、参数、边界条件和正则性。

### Unknown Answer Slots

#### 1. 原问题的主要数学真相

需要确定目标结论究竟成立、失败还是依赖参数区域，并把存在性、唯一性与分类边界分开处理。离线模式不预设任何候选答案。

#### 2. 完成答案所需的最弱中间结论

需要识别哪些 representation、estimate、compactness、construction 或 obstruction 真正足以推进原问题，避免把研究升级为远强于所需的定理。

#### 3. 部分但有独立价值的结果

即使完整目标暂时未闭合，也应判断哪些 sharp bounds、特殊情形、条件性 theorem 或严格障碍值得保留。

### Full-Answer Outcome Tree

#### 分支一：原目标成立

需要形成自包含论证，说明适用范围、量词、参数依赖和必要的唯一性或全局性步骤。

#### 分支二：原目标失败

需要给出满足全部前提的严格反例或结构性否定，并说明仍可能成立的较准确条件。

#### 分支三：答案依赖参数或 regime

需要给出参数区域或渐近路径的分类，不能以单个特殊情形代替全局答案。

### Scope and Forbidden Weakening

- 不得增加用户未授权假设。
- 不得把特殊参数或单个子序列冒充完整回答。
- 不得把未经验证候选写成 theorem。

## B. Research Policy

### Hard Methodological Constraints

- 不引入 Lean。
- 不以大规模数值模拟替代严格数学论证。
- 正式结论必须遵守原问题的对象、量词和参数边界。

### Soft Research Preferences

- 优先获得高信息增益的结构判断。
- 同时保留正面机制与反例压力测试。
- 中间定理优先采用 minimal sufficient formulation。

### Partial Research Yields Worth Preserving

- 非平凡上下界或参数区域结果；
- 特殊但自然情形的完整 theorem；
- 条件性结果与精确剩余缺口；
- 可独立陈述的结构性 obstruction。

## C. Structural Diagnosis

离线模式只建立产品演示结构。真实 Contractor 会在 Stage 1 中先做数学诊断，再将少量核心未知、candidate mechanisms 与 minimal bridges 写入正式 Contract。

## D. 第一层：立即启动的 Research Nodes

### RN-01（Discovery）：原问题的结构与候选机制

**和原问题的关系**

该节点用于识别原问题的主要真相分支和最小必要中间结论；正面结果会产生可研究的 Claim，负面结果会排除错误 framing。

**当前目标**

恢复数学对象、量词和候选机制，形成至少一个可精确陈述的候选结论或结构性障碍。

**当前输入**

用户原始 Research Request 与可用 Source Materials；离线模式不增加额外数学假设。

**首轮方式**

先正规化题面并检查最简单模型，再比较正面证明、构造反例和标准理论接口三类路线，依据哪一类最先形成精确数学对象决定后续分叉。

## E. 第二层：预计随后启动的 Research Directions

### RD-01：候选结论的严格化

**和原问题的关系**

候选若能被严格化，将把开放问题转化为具体 Claim；若候选失败，则需要分类失败机制。

**触发条件**

当第一阶段已经形成一个具有明确对象、量词和适用范围的候选结论或严格障碍时启动。

**大致目标与路径**

把候选拆成最弱必要 Lemmas，分别研究证明、反例和参数边界；候选仍不明确时不提前伪造 theorem。

## F. 后续研究方向与终局任务

### 1. 完整答案的唯一性与全局性

**和原问题的关系**

候选成立后仍需排除其他可能答案，并处理全序列、全参数或全对象类层面的结论。

### 2. Sharpness 与边界情形

**和原问题的关系**

主结论形成后需要判断条件是否必要、端点是否成立，以及是否存在揭示最优性的反例。

### 3. 更一般对象类或较低正则性

**和原问题的关系**

若当前结论依赖较强条件，可进一步研究哪些假设只是技术性的，哪些真正决定数学现象。
"""

_OFFLINE_CONTRACT_CHAT = """# Contract Chat Response

## Reply

离线演示模式下未调用真实 Contract Chat 模型；当前 Working Draft 保持不变。

## Draft Patches

NONE
"""

_OFFLINE_SUPERVISOR = """# Node Supervisor Review

## Current Diagnosis

离线演示模式。

## Worker Assignments

### literature-1
Persona: literature
Task: 恢复一个最相关 theorem interface，并核对适用边界。

### construction-1
Persona: construction
Task: 构造候选对象或攻击当前认识。

### general-proof-1
Persona: general_proof
Task: 推导一般性必要条件或正面论证。

### cross-domain-1
Persona: cross_domain
Task: 寻找新的表示、机制或降维。

## Research Findings

暂无。

## Promotion Proposals

暂无。

## Architecture Communications

暂无。

## Supervisor Notes

离线模式没有需要跨轮保留的数学记忆。

## Node Decision

状态：继续研究
架构关注：无
"""

_OFFLINE_WORKER = """# Worker Research Report

## Research Content

离线演示模式没有执行真实数学研究。

## 任务状态

状态：未完成

## 重大事件

无
"""

_OFFLINE_ARCHITECTURE = """# Fresh Research Architecture Review

## Global Diagnosis

离线演示模式。

## Evidence Inspected On Demand

无。

## Actions

暂无。

## Promotion Decisions

无。

## Architecture Snapshot Update

无需更新。

## Decision Ledger Updates

暂无。

## Deferred Findings

暂无。
"""

_OFFLINE_VERIFIER = """# Fresh Claim Verification

## Verdict

REJECT

## Exact Statement

离线演示模式没有可被独立核验的数学陈述。

## Verified Mathematical Result

不适用。

## Assumption and Dependency Check

离线演示模式没有外部数学依赖。

## Decisive Mathematical Analysis

离线演示模式无法核验数学候选。

## Reuse Boundary

不适用。
"""


_OFFLINE_SUPERVISOR_REVIEW = """# Node Supervisor Review

## Current Diagnosis

离线演示已完成一轮四 Persona 报告；本 Node 不再创建新任务。

## Worker Assignments

无。

## Worker Retirements

无。

## Research Findings

暂无。

## Architecture Communications

暂无。

## Supervisor Notes

离线演示已完成当前 Node 的局部责任。

## Discovery Completion Assessment

离线演示已完成既定结构检查，并把尚未验证的数学义务明确留给后续真实研究。

## Node Decision

状态：节点完成
架构关注：无
"""

_OFFLINE_REPORT = FIXED_LATEX_PREAMBLE + r"""\title{A Conditional Note on an Unresolved Mathematical Problem}
\author{}
\date{}
\begin{document}
\maketitle
\begin{abstract}
This note records the mathematical structure of an unresolved problem without
promoting an unverified mechanism to a theorem.  The available material does not
contain a complete nontrivial proof.  We therefore separate the exact problem,
the conditional interfaces that a proof would have to establish, and the
remaining analytic obstruction.
\end{abstract}

\section{Introduction}
An incomplete mathematical investigation can still be organized as a useful
paper provided that its evidence boundary is explicit.  The purpose of this note
is not to claim a solution, but to distinguish definitions, potentially
verifiable intermediate statements, and the point at which a genuine argument is
still missing.  Formal analogies and finite calculations are not treated as
proofs.

\section{Mathematical Setting}
Let the object under study satisfy the regularity, boundary, and parameter
conditions in the original problem statement.  At present the supplied material
does not justify a sharper theorem.  These hypotheses therefore delimit the
question but are not used here to infer an unsupported conclusion.

\section{Conditional Structure}
A complete solution would require at least two logically independent steps.  One
must first derive the proposed identity or structural constraint directly from
the governing equations.  One must then prove that this constraint holds for the
entire admissible class and establish the reverse implication when the problem
asks for an equivalence.  If either step is absent, the mechanism remains a
research interface rather than a theorem.

The safe conditional statement is the following.  If the required identity and
its localization property can be proved, then the target classification may
reduce to rigidity of the relevant coefficient or parameter.  If localization
fails, one instead needs an explicit counterexample or an additional hypothesis.
This logical alternative does not decide which branch is valid.

\section{Remaining Mathematical Issue}
The central gap is a rigorous derivation from the original equations together
with a complete accounting of regularity, boundary terms, and quantifiers.  A
special example or numerical experiment cannot replace this step.  Any future
claim must identify exactly where every assumption enters and whether necessity
and sufficiency have both been established.

\section{Discussion}
The next useful task is to specify the admissible test class, calculate all
boundary contributions, and isolate a local criterion capable of separating the
two logical directions.  If those steps do not close, the correct conclusion is
that the problem remains open under the stated assumptions.  This presentation
preserves that distinction and leaves a precise interface for subsequent work.
\end{document}
"""

_OFFLINE_REPORT_AUDIT = """# Fresh Mathematical Manuscript Audit

## Verdict

PASS

## Mathematical Correctness

未发现无证据数学声明。

## Evidence Boundary

稿件没有把条件性接口写成定理，也没有越过可用数学材料。

## Manuscript Genre

稿件以数学问题、条件结构和开放缺口组织，没有使用内部运行语言。

## References

未提供真实外部来源，稿件正确省略参考文献。

## Required Revisions

无。

无需修改。
"""

_OFFLINE_PRIOR_ART_SCOUT = """# Prior-Art Assessment

## 1. What the manuscript appears to claim

离线模式不执行外部文献检索。

## 2. Search directions pursued

无。

## 3. Closest fully read sources

无。

## 4. Overall prior-art judgment

没有可供判断的实时文献证据。

## 5. Suggested manuscript changes

采用保守的 novelty 表述。

## 6. Additional literature verified at abstract level

无。
"""

_OFFLINE_CONTEXT_SCOUT = """# Related-Work and Mathematical Context Report

## 1. Mathematical placement of the manuscript

离线模式不执行外部文献检索。

## 2. Literature strands that matter

无。

## 3. Fully read sources

无。

## 4. Suggested literature narrative for the manuscript

没有可供使用的实时文献材料。

## 5. Focused manuscript suggestions

保持背景陈述保守。

## 6. Additional literature verified at abstract level

无。
"""
