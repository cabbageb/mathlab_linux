from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import uuid

from .config import AppConfig, ModelConfig
from .codex_permissions import (
    PROJECT_ROOT_MARKER,
    ensure_permission_profiles_supported,
    managed_runtime_path,
    managed_runtime_workspace,
    permission_profile_name,
    render_permission_profiles,
    write_generated_codex_config,
)
from .models import TurnResult
from .thread_compactor import CodexThreadCompactor


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
_STREAM_LIMIT = 16 * 1024 * 1024


class CodexError(RuntimeError):
    pass


@dataclass
class ExecSession:
    model: ModelConfig
    cwd: Path
    instructions: str
    persistent: bool
    thread_id: str | None = None


class CodexExecClient:
    def __init__(self, config: AppConfig, event_handler: EventHandler | None = None) -> None:
        self.config = config
        self.event_handler = event_handler
        self.codex_home = config.runtime_dir / "codex_home"
        self.sessions: dict[str, ExecSession] = {}
        self.processes: set[asyncio.subprocess.Process] = set()
        self._process_waiters: dict[
            asyncio.subprocess.Process, asyncio.Task[int]
        ] = {}
        self.compactor = CodexThreadCompactor(config, self.codex_home)
        self._model_windows: dict[str, int] | None = None
        self._pause_requested = False
        self._pause_reason = ""

    async def start(self) -> None:
        """检查 CLI、密钥和模型目录，并生成 Prover 专属 CODEX_HOME。"""
        if not shutil.which(self.config.codex.binary):
            raise CodexError(f"找不到 Codex CLI：{self.config.codex.binary}")
        try:
            ensure_permission_profiles_supported(self.config.codex.binary)
        except RuntimeError as exc:
            raise CodexError(str(exc)) from exc
        if not os.environ.get(self.config.codex.api_key_env):
            raise CodexError(f"环境变量 {self.config.codex.api_key_env} 未设置")
        if not self.config.codex.model_catalog.is_file():
            raise CodexError("DeepSeek 模型目录不存在")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._write_config()

    async def stop(self) -> None:
        """终止本进程仍维护的全部 Codex 子进程。"""
        for process in list(self.processes):
            self._signal(process, signal.SIGTERM)
        if self.processes:
            await asyncio.gather(
                *(self._wait_or_kill(process) for process in list(self.processes)),
                return_exceptions=True,
            )
        await self._terminate_scoped_sandbox_helpers()
        await self.compactor.stop()

    async def pause_active_turns(self, reason: str) -> None:
        """分批停止 scoped turn，并让各 run_turn 自己完成唯一进程回收。"""
        self._pause_requested = True
        self._pause_reason = reason
        processes = list(self.processes)
        batch_size = 4
        for start in range(0, len(processes), batch_size):
            for process in processes[start:start + batch_size]:
                self._signal(process, signal.SIGINT)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.25)
        remaining = [process for process in processes if process.returncode is None]
        for start in range(0, len(remaining), batch_size):
            for process in remaining[start:start + batch_size]:
                self._signal(process, signal.SIGTERM)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.25)
        remaining = [process for process in remaining if process.returncode is None]
        for start in range(0, len(remaining), batch_size):
            for process in remaining[start:start + batch_size]:
                self._signal(process, signal.SIGKILL)
            await asyncio.sleep(0.05)
        await self._terminate_scoped_sandbox_helpers()

    def _scoped_sandbox_helper_pids(
        self,
        proc_root: Path = Path("/proc"),
        *,
        scope: Path | None = None,
    ) -> list[int]:
        """只查找命令行明确绑定当前 Run runtime 的孤立 bwrap helper。"""
        marker = str((scope or self.config.runtime_dir).resolve()).encode()
        matches: list[int] = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                executable = (entry / "exe").resolve(strict=True)
                command = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if executable.name == "bwrap" and marker in command:
                matches.append(int(entry.name))
        return sorted(matches)

    async def _terminate_scoped_sandbox_helpers(
        self, scope: Path | None = None
    ) -> None:
        """回收 Codex shell 在独立进程组中遗留的指定 workspace bwrap。"""
        pids = self._scoped_sandbox_helper_pids(scope=scope)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            remaining: list[int] = []
            for pid in pids:
                try:
                    os.kill(pid, sig)
                    remaining.append(pid)
                except ProcessLookupError:
                    continue
            pids = remaining
            if not pids:
                return
            for _ in range(10):
                await asyncio.sleep(0.05)
                alive: list[int] = []
                for pid in pids:
                    try:
                        os.kill(pid, 0)
                        alive.append(pid)
                    except ProcessLookupError:
                        continue
                pids = alive
                if not pids:
                    return

    def resume_turns(self) -> None:
        """解除新 turn 的 scoped pause barrier。"""
        self._pause_requested = False
        self._pause_reason = ""

    async def compact_thread(self, thread_id: str) -> None:
        """通过同一 CODEX_HOME 显式压缩已经登记的持久 thread。"""
        session = self.sessions.get(thread_id)
        if session is None:
            raise CodexError(f"未登记待压缩 Codex thread：{thread_id}")
        await self.compactor.compact(
            thread_id,
            model=session.model,
            cwd=session.cwd,
            instructions=session.instructions,
        )

    async def start_thread(
        self,
        *,
        model: ModelConfig,
        cwd: Path,
        instructions: str,
        persistent: bool,
    ) -> str:
        """登记一个逻辑会话，首轮时再由 codex exec 创建真实 thread。"""
        logical = f"pending-{uuid.uuid4()}"
        workspace = managed_runtime_workspace(self.config, cwd, "角色工作目录")
        self.sessions[logical] = ExecSession(
            model=model,
            cwd=workspace,
            instructions=instructions,
            persistent=persistent,
        )
        return logical

    async def resume_thread(
        self,
        thread_id: str,
        *,
        model: ModelConfig,
        cwd: Path,
        instructions: str,
    ) -> None:
        """登记磁盘中恢复的真实 thread，并刷新本轮角色设置。"""
        workspace = managed_runtime_workspace(self.config, cwd, "角色工作目录")
        session = self.sessions.get(thread_id)
        if session is None:
            real_thread = None if thread_id.startswith("pending-") else thread_id
            session = ExecSession(model, workspace, instructions, True, real_thread)
            self.sessions[thread_id] = session
        else:
            session.model = model
            session.cwd = workspace
            session.instructions = instructions
            session.thread_id = None if thread_id.startswith("pending-") else thread_id

    def resolved_thread_id(self, logical: str) -> str | None:
        session = self.sessions.get(logical)
        return session.thread_id if session else None

    async def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: ModelConfig,
        record_dir: Path,
        on_thread_ready: Callable[[str], Awaitable[None]] | None = None,
        timeout_seconds: int | None = None,
        timeout_status: str | None = None,
    ) -> TurnResult:
        """执行或恢复一轮 Codex，并完整保存 stdout JSONL、stderr 和最终消息。"""
        session = self.sessions.get(thread_id)
        if session is None:
            raise CodexError(f"未登记 Codex thread：{thread_id}")
        if self._pause_requested:
            return TurnResult(
                thread_id=session.thread_id or thread_id,
                status="paused",
                text="",
                usage={},
                error=self._pause_reason or "Research Run paused",
            )
        session.cwd = managed_runtime_workspace(
            self.config, session.cwd, "角色工作目录"
        )
        record_dir = managed_runtime_path(self.config, record_dir, "turn 记录目录")
        record_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = record_dir / "instructions.md"
        instructions_path.write_text(session.instructions.rstrip() + "\n", encoding="utf-8")
        final_path = record_dir / "final_message.md"
        stream_path = record_dir / "stream.jsonl"
        stderr_path = record_dir / "stderr.log"
        command = self._command(session, model, instructions_path, final_path)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session.cwd,
            env=self._environment(),
            start_new_session=True,
            limit=_STREAM_LIMIT,
        )
        self.processes.add(process)
        process_task = self._process_waiter(process)
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        real_thread = session.thread_id or ""
        usage: dict[str, int] = {}
        latest_text = ""
        latest_error = ""
        stderr_tail = ""
        turn_completed = False

        async def read_stdout() -> None:
            nonlocal real_thread, usage, latest_text, latest_error, turn_completed
            with stream_path.open("a", encoding="utf-8") as handle:
                while line := await process.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace")
                    handle.write(decoded)
                    handle.flush()
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError:
                        continue
                    kind = str(event.get("type") or "")
                    if kind == "thread.started":
                        real_thread = str(event.get("thread_id") or real_thread)
                        if real_thread:
                            session.thread_id = real_thread
                            self.sessions[real_thread] = session
                            if on_thread_ready is not None:
                                await on_thread_ready(real_thread)
                    elif kind == "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "agent_message":
                            latest_text = str(item.get("text") or latest_text)
                    elif kind == "turn.completed":
                        turn_completed = True
                        usage = {
                            key: int(value or 0)
                            for key, value in (event.get("usage") or {}).items()
                            if isinstance(value, (int, float))
                        }
                        usage.setdefault(
                            "model_context_window", self.model_context_window(model.model)
                        )
                    elif kind in {"turn.failed", "error"}:
                        raw = event.get("error") or event.get("message") or event
                        latest_error = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    if self.event_handler:
                        await self.event_handler(thread_id, event)

        async def read_stderr() -> None:
            nonlocal stderr_tail
            with stderr_path.open("ab") as handle:
                while line := await process.stderr.readline():
                    handle.write(line)
                    handle.flush()
                    stderr_tail = (stderr_tail + line.decode("utf-8", errors="replace"))[-4000:]

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        turn_tasks = (process_task, stdout_task, stderr_task)
        effective_timeout = (
            self.config.role_call_timeout_seconds
            if timeout_seconds is None
            else (None if timeout_seconds <= 0 else timeout_seconds)
        )
        try:
            _, pending = await asyncio.wait(
                turn_tasks,
                timeout=effective_timeout,
            )
            if pending:
                raise asyncio.TimeoutError
            return_code = process_task.result()
            stdout_task.result()
            stderr_task.result()
        except asyncio.TimeoutError as exc:
            self._signal(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in turn_tasks:
                task.cancel()
            await asyncio.gather(*turn_tasks, return_exceptions=True)
            if real_thread:
                session.thread_id = real_thread
                self.sessions[real_thread] = session
            if timeout_status is None:
                raise CodexError(f"模型调用超过 {effective_timeout} 秒") from exc
            final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else latest_text
            return TurnResult(
                thread_id=real_thread or (
                    "" if thread_id.startswith("pending-") else thread_id
                ),
                status=timeout_status,
                text=final_text,
                usage=usage,
                error=f"模型调用达到阶段截止点 {effective_timeout} 秒",
                stream_path=str(stream_path),
            )
        except asyncio.CancelledError:
            self._signal(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in turn_tasks:
                task.cancel()
            await asyncio.gather(*turn_tasks, return_exceptions=True)
            raise
        except Exception:
            self._signal(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in turn_tasks:
                task.cancel()
            await asyncio.gather(*turn_tasks, return_exceptions=True)
            raise
        finally:
            self.processes.discard(process)
            self._process_waiters.pop(process, None)
            await self._terminate_scoped_sandbox_helpers(session.cwd)
        if real_thread:
            session.thread_id = real_thread
            self.sessions[real_thread] = session
        final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else latest_text
        intentionally_paused = self._pause_requested and not turn_completed
        error = None if return_code == 0 else (latest_error.strip() or stderr_tail.strip() or f"exit={return_code}")
        return TurnResult(
            thread_id=real_thread or (
                "" if thread_id.startswith("pending-") else thread_id
            ),
            status="paused" if intentionally_paused else ("completed" if return_code == 0 else "failed"),
            text=final_text,
            usage=usage,
            error=(self._pause_reason or "Research Run paused") if intentionally_paused else error,
            stream_path=str(stream_path),
        )

    def model_context_window(self, model_slug: str) -> int:
        """从隔离模型目录读取指定模型的上下文窗口。"""
        if self._model_windows is None:
            payload = json.loads(self.config.codex.model_catalog.read_text(encoding="utf-8"))
            self._model_windows = {
                str(item.get("slug")): int(item.get("context_window") or 0)
                for item in payload.get("models") or []
            }
        return self._model_windows.get(model_slug, 0)

    def _command(
        self,
        session: ExecSession,
        model: ModelConfig,
        instructions_path: Path,
        final_path: Path,
    ) -> list[str]:
        """构造新建或 resume 命令，并固定模型及原生硬权限档案。"""
        binary = shutil.which(self.config.codex.binary) or self.config.codex.binary
        base = [
            binary,
            "--ask-for-approval",
            "never",
            "exec",
            "--dangerously-bypass-hook-trust",
        ]
        if self.config.codex.use_docker_boundary:
            base += ["--sandbox", "danger-full-access"]
        network_access = (
            "<!-- controller-network-access: enabled -->" in session.instructions
        )
        permission_profile = permission_profile_name(
            network_access=network_access
        )
        common = [
            "--json",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--model",
            model.model,
            "-c",
            f"model_reasoning_effort={json.dumps(model.reasoning_effort)}",
            "-c",
            f"model_context_window={self.model_context_window(model.model)}",
            "-c",
            (
                "model_auto_compact_token_limit="
                f"{self.config.codex.model_auto_compact_token_limit}"
            ),
            "-c",
            "features.multi_agent=false",
            "-c",
            "features.memories=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.skill_search=false",
            "-c",
            "features.shell_tool=true",
            "-c",
            "features.unified_exec=false",
            "-c",
            "web_search=\"disabled\"",
            "-c",
            f"model_instructions_file={json.dumps(str(instructions_path))}",
            "-o",
            str(final_path),
        ]
        if not self.config.codex.use_docker_boundary:
            common += ["-c", f"default_permissions={json.dumps(permission_profile)}"]
        if session.thread_id:
            return base + ["resume"] + common + [session.thread_id, "-"]
        command = base + common
        if not session.persistent:
            command.append("--ephemeral")
        command += ["-C", str(session.cwd), "-"]
        return command

    def _write_config(self) -> None:
        """生成不继承用户 Skills、memory 或其他项目规则的隔离配置。"""
        default = self.config.models["research_literature"]
        sandbox_config = (
            'sandbox_mode = "danger-full-access"'
            if self.config.codex.use_docker_boundary
            else render_permission_profiles(self.config).rstrip()
        )
        hook_script = Path(__file__).with_name("force_shell_workspace_hook.py").resolve()
        hook_source = hook_script.read_text(encoding="utf-8")
        hook_command = (
            f"{shlex.quote(sys.executable)} -c "
            f"{shlex.quote(hook_source)}"
        )
        content = f'''model = {json.dumps(default.model)}
model_provider = {json.dumps(self.config.codex.provider)}
model_reasoning_effort = {json.dumps(default.reasoning_effort)}
model_catalog_json = {json.dumps(str(self.config.codex.model_catalog))}
model_auto_compact_token_limit = {self.config.codex.model_auto_compact_token_limit}
approval_policy = "never"
project_root_markers = [{json.dumps(PROJECT_ROOT_MARKER)}]
web_search = "disabled"

{sandbox_config}

[skills]
include_instructions = false

[model_providers.{self.config.codex.provider}]
name = "Configured provider"
base_url = {json.dumps(self.config.codex.base_url)}
env_key = {json.dumps(self.config.codex.api_key_env)}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false

[features]
multi_agent = false
memories = false
plugins = false
skill_search = false
skill_mcp_dependency_install = false
shell_snapshot = false
shell_tool = true
unified_exec = false
hooks = true

[[hooks.PreToolUse]]
matcher = ".*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = {json.dumps(hook_command)}
timeout = 5

[history]
persistence = {json.dumps(self.config.codex.history_persistence)}

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false
exclude = ["*KEY*", "*SECRET*", "*TOKEN*", "*PASSWORD*", "DASHSCOPE_*", "OPENAI_*", "ADVANCEDSOLVER_*", "MATHLAB_PROVIDER_*"]
include_only = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TERM", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "THEOREM_SEARCH_PRIMARY", "THEOREM_SEARCH_FALLBACK", "ARXIV_API_ENDPOINT"]
'''
        write_generated_codex_config(self.codex_home / "config.toml", content)
        (self.codex_home / "hooks.json").unlink(missing_ok=True)

    def _environment(self) -> dict[str, str]:
        """让受限 Agent shell 复用启动 MathLab 的同一 Python 环境。"""
        env = dict(os.environ)
        python_bin = str(Path(sys.executable).resolve().parent)
        inherited_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(
            item for item in (python_bin, inherited_path) if item
        )
        env["CODEX_HOME"] = str(self.codex_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["THEOREM_SEARCH_PRIMARY"] = self.config.search.theorem_primary_endpoint
        env["THEOREM_SEARCH_FALLBACK"] = self.config.search.theorem_fallback_endpoint
        env["ARXIV_API_ENDPOINT"] = self.config.search.arxiv_api_endpoint
        return env

    @staticmethod
    def _signal(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def _process_waiter(
        self,
        process: asyncio.subprocess.Process,
    ) -> asyncio.Task[int]:
        """为一个子进程创建或复用唯一 wait task，避免并发 wait 死锁。"""
        waiter = self._process_waiters.get(process)
        if waiter is None:
            waiter = asyncio.create_task(process.wait())
            self._process_waiters[process] = waiter
        return waiter

    async def _wait_or_kill(self, process: asyncio.subprocess.Process) -> None:
        waiter = self._process_waiter(process)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=3)
        except TimeoutError:
            self._signal(process, signal.SIGKILL)
            await asyncio.wait_for(asyncio.shield(waiter), timeout=3)
