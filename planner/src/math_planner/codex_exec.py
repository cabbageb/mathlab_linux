from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import shutil
import signal
import uuid

from .config import AppConfig, ModelConfig
from .models import TurnResult


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


# Codex 的 command_execution 完成事件会把命令输出放进同一条 JSONL；完整
# Plan 输入可轻易超过 asyncio 默认的 64 KiB 单行限制。
_JSONL_STREAM_LIMIT = 16 * 1024 * 1024


class CodexExecError(RuntimeError):
    pass


@dataclass
class ExecSession:
    """保存一次 exec 会话的启动参数，并在首轮完成后记录真实 thread id。"""

    model: ModelConfig
    cwd: Path
    developer_instructions: str | None
    sandbox: str
    ephemeral: bool
    thread_id: str | None = None


class CodexExecClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        message_handler: MessageHandler | None = None,
    ) -> None:
        self.config = config
        self.message_handler = message_handler
        self._sessions: dict[str, ExecSession] = {}
        self._processes: set[asyncio.subprocess.Process] = set()
        self._codex_home = self.config.runtime_dir / "codex_home"

    async def start(self) -> None:
        """检查 Codex 与密钥，并生成 math_planner 专属的无密钥配置。"""
        if not shutil.which(self.config.codex.binary):
            raise CodexExecError(f"找不到 Codex CLI：{self.config.codex.binary}")
        if not os.environ.get(self.config.codex.api_key_env):
            raise CodexExecError(
                f"环境变量 {self.config.codex.api_key_env} 未设置或为空"
            )
        self._codex_home.mkdir(parents=True, exist_ok=True)
        self._write_codex_config()

    async def stop(self) -> None:
        """终止仍在运行的全部 exec 进程组，避免网页退出后留下计费调用。"""
        processes = list(self._processes)
        for process in processes:
            self._signal_process_group(process, signal.SIGTERM)
        if processes:
            await asyncio.gather(
                *(self._wait_or_kill(process) for process in processes),
                return_exceptions=True,
            )

    async def start_thread(
        self,
        *,
        model: ModelConfig,
        cwd: Path,
        developer_instructions: str | None,
        sandbox: str = "read-only",
        ephemeral: bool = False,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """登记一个待启动会话；真正的 Codex thread 在第一次 run_turn 时创建。"""
        if dynamic_tools:
            raise CodexExecError("codex exec 不支持客户端注册的动态工具")
        session_id = f"pending-{uuid.uuid4()}"
        self._sessions[session_id] = ExecSession(
            model=model,
            cwd=cwd.resolve(),
            developer_instructions=developer_instructions,
            sandbox=sandbox,
            ephemeral=ephemeral,
        )
        return session_id

    async def resume_thread(
        self,
        thread_id: str,
        *,
        model: ModelConfig | None = None,
        cwd: Path | None = None,
        developer_instructions: str | None = None,
        sandbox: str = "read-only",
    ) -> None:
        """为重启后的持久 thread 恢复工作区和角色设置，随后由 exec resume 续接。"""
        session = self._sessions.get(thread_id)
        if session is None:
            real_thread = None if thread_id.startswith("pending-") else thread_id
            self._sessions[thread_id] = ExecSession(
                model=model or self.config.models["contractor"],
                cwd=(cwd or self.config.root).resolve(),
                developer_instructions=developer_instructions,
                sandbox=sandbox,
                ephemeral=False,
                thread_id=real_thread,
            )
            return
        if model is not None:
            session.model = model
        if cwd is not None:
            session.cwd = cwd.resolve()
        if developer_instructions is not None:
            session.developer_instructions = developer_instructions
        session.sandbox = sandbox
        session.thread_id = None if thread_id.startswith("pending-") else thread_id

    def resolved_thread_id(self, thread_id: str) -> str | None:
        """返回已从事件流解析出的真实 Codex thread id。"""
        session = self._sessions.get(thread_id)
        return session.thread_id if session is not None else None

    async def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: ModelConfig,
        cwd: Path | None = None,
        record_dir: Path | None = None,
        on_thread_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> TurnResult:
        """在稳定工作区执行一轮，并把本轮原始日志写到独立记录目录。"""
        session = self._sessions.get(thread_id)
        if session is None:
            session = ExecSession(
                model=model,
                cwd=(cwd or self.config.root).resolve(),
                developer_instructions=None,
                sandbox="read-only",
                ephemeral=False,
                thread_id=thread_id,
            )
            self._sessions[thread_id] = session
        workspace_dir = (cwd or session.cwd).resolve()
        log_dir = (record_dir or workspace_dir).resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        instructions_path = log_dir / "instructions.md"
        if session.developer_instructions:
            instructions_path.write_text(
                session.developer_instructions.rstrip() + "\n", encoding="utf-8"
            )
        final_path = log_dir / "final_message.md"
        stream_path = log_dir / "stream.jsonl"
        stderr_path = log_dir / "stderr.log"
        command = self._build_command(
            session=session,
            model=model,
            run_dir=workspace_dir,
            instructions_path=instructions_path,
            final_path=final_path,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=self._environment(),
            start_new_session=True,
            limit=_JSONL_STREAM_LIMIT,
        )
        self._processes.add(process)
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        usage: dict[str, int] = {}
        real_thread_id = session.thread_id or ""
        latest_agent_text = ""
        latest_error = ""
        stderr_tail = ""

        async def read_stdout() -> None:
            nonlocal real_thread_id, latest_agent_text, latest_error, usage
            with stream_path.open("a", encoding="utf-8") as stream:
                while line := await process.stdout.readline():
                    decoded = line.decode("utf-8", errors="replace")
                    stream.write(decoded)
                    stream.flush()
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "thread.started":
                        real_thread_id = str(event.get("thread_id") or real_thread_id)
                        if real_thread_id:
                            session.thread_id = real_thread_id
                            self._sessions[real_thread_id] = session
                            if on_thread_started is not None:
                                await on_thread_started(real_thread_id)
                    elif event.get("type") == "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "agent_message":
                            latest_agent_text = str(item.get("text") or latest_agent_text)
                    elif event.get("type") == "turn.completed":
                        usage = {
                            key: int(value or 0)
                            for key, value in (event.get("usage") or {}).items()
                        }
                    elif event.get("type") in {"turn.failed", "error"}:
                        raw_error = event.get("error") or event.get("message") or event
                        latest_error = (
                            raw_error
                            if isinstance(raw_error, str)
                            else json.dumps(raw_error, ensure_ascii=False)
                        )
                    await self._forward_event(thread_id, event)

        async def read_stderr() -> None:
            nonlocal stderr_tail
            with stderr_path.open("ab") as stderr:
                while line := await process.stderr.readline():
                    stderr.write(line)
                    stderr.flush()
                    stderr_tail = (stderr_tail + line.decode("utf-8", errors="replace"))[-4000:]

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        process_task = asyncio.create_task(process.wait())
        try:
            return_code, _, _ = await asyncio.wait_for(
                asyncio.gather(process_task, stdout_task, stderr_task),
                timeout=self.config.role_call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._signal_process_group(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in (process_task, stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(
                process_task, stdout_task, stderr_task, return_exceptions=True
            )
            raise CodexExecError(
                f"模型调用超过 {self.config.role_call_timeout_seconds} 秒"
            ) from exc
        except asyncio.CancelledError:
            self._signal_process_group(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in (process_task, stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(
                process_task, stdout_task, stderr_task, return_exceptions=True
            )
            raise
        except Exception as exc:
            self._signal_process_group(process, signal.SIGTERM)
            await self._wait_or_kill(process)
            for task in (process_task, stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(
                process_task, stdout_task, stderr_task, return_exceptions=True
            )
            raise CodexExecError(
                f"模型事件流读取失败：{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            self._processes.discard(process)

        if real_thread_id:
            session.thread_id = real_thread_id
            self._sessions[real_thread_id] = session
        final_text = (
            final_path.read_text(encoding="utf-8")
            if final_path.exists()
            else latest_agent_text
        )
        status = "completed" if return_code == 0 else "failed"
        error = None
        if return_code != 0:
            detail = latest_error.strip() or stderr_tail.strip()
            error = detail or f"codex exec exited with code {return_code}"
        return TurnResult(
            thread_id=real_thread_id or (
                "" if thread_id.startswith("pending-") else thread_id
            ),
            turn_id=f"exec-{uuid.uuid4()}",
            status=status,
            text=final_text,
            item_type="agentMessage",
            usage=usage,
            error=error,
        )

    def _build_command(
        self,
        *,
        session: ExecSession,
        model: ModelConfig,
        run_dir: Path,
        instructions_path: Path,
        final_path: Path,
    ) -> list[str]:
        """构造新会话或 resume 会话命令，并保持 provider 与推理档位一致。"""
        binary = shutil.which(self.config.codex.binary) or self.config.codex.binary
        sandbox = (
            "danger-full-access"
            if self.config.codex.use_docker_boundary
            else session.sandbox
        )
        base = [binary, "--ask-for-approval", "never", "exec"]
        common = [
            "--json",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--model",
            model.model,
            "-c",
            f"model_reasoning_effort={json.dumps(model.reasoning_effort)}",
            "-c",
            f"sandbox_mode={json.dumps(sandbox)}",
            "-c",
            "features.multi_agent=false",
            "-c",
            "features.memories=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.skill_search=false",
            "-c",
            "features.skill_mcp_dependency_install=false",
            "-c",
            "features.shell_snapshot=false",
            "-c",
            "web_search=\"disabled\"",
            "-o",
            str(final_path),
        ]
        if session.developer_instructions:
            common += [
                "-c",
                f"model_instructions_file={json.dumps(str(instructions_path))}",
            ]
        if session.thread_id:
            return base + ["resume"] + common + [session.thread_id, "-"]
        command = base + common
        if session.ephemeral:
            command.append("--ephemeral")
        command += ["--sandbox", sandbox, "-C", str(run_dir)]
        command.append("-")
        return command

    async def _forward_event(self, logical_thread_id: str, event: dict[str, Any]) -> None:
        """把 exec JSONL 事件规范化成 Controller 已使用的轻量事件结构。"""
        if not self.message_handler:
            return
        event_type = str(event.get("type", ""))
        if event_type == "thread.started":
            await self.message_handler(
                {"method": "turn/started", "params": {"threadId": logical_thread_id}}
            )
        elif event_type == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type")
            normalized: dict[str, Any] | None = None
            if item_type == "agent_message":
                normalized = {
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": item.get("text", ""),
                }
            elif item_type == "command_execution":
                normalized = {
                    "type": "commandExecution",
                    "status": item.get("status"),
                    "exitCode": item.get("exit_code"),
                }
            if normalized is not None:
                await self.message_handler(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": logical_thread_id,
                            "item": normalized,
                        },
                    }
                )
        elif event_type == "turn.completed":
            await self.message_handler(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": logical_thread_id,
                        "turn": {"status": "completed"},
                    },
                }
            )
        elif event_type in {"turn.failed", "error"}:
            # 同一次失败通常同时产生 error 与 turn.failed。原始 JSONL 已完整写盘，
            # 最终失败或重试状态统一由 Controller 根据 TurnResult 发布，避免重复提示。
            return

    def _write_codex_config(self) -> None:
        """写入独立 CODEX_HOME 配置，不继承父目录 skills、memories 或 AGENTS。"""
        sandbox = (
            "danger-full-access"
            if self.config.codex.use_docker_boundary
            else "read-only"
        )
        workspace_config = (
            ""
            if self.config.codex.use_docker_boundary
            else "[sandbox_workspace_write]\nnetwork_access = true\n"
        )
        content = f'''model = {json.dumps(self.config.models["contractor"].model)}
model_provider = {json.dumps(self.config.codex.provider)}
model_reasoning_effort = {json.dumps(self.config.models["contractor"].reasoning_effort)}
model_catalog_json = {json.dumps(str(self.config.codex.model_catalog))}
model_context_window = {self.config.codex.model_context_window}
model_auto_compact_token_limit = {self.config.codex.model_auto_compact_token_limit}
approval_policy = "never"
sandbox_mode = {json.dumps(sandbox)}
project_root_markers = [".math_planner_root"]
web_search = "disabled"

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

[history]
persistence = {json.dumps(self.config.codex.history_persistence)}

{workspace_config}

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false
exclude = ["*KEY*", "*SECRET*", "*TOKEN*", "*PASSWORD*", "DASHSCOPE_*", "OPENAI_*", "ADVANCEDSOLVER_*", "MATHLAB_PROVIDER_*"]
include_only = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TERM", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "MATH_PLANNER_ROOT", "THEOREM_SEARCH_PRIMARY", "THEOREM_SEARCH_FALLBACK", "ARXIV_API_ENDPOINT"]
'''
        (self._codex_home / "config.toml").write_text(content, encoding="utf-8")

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self._codex_home)
        env["MATH_PLANNER_ROOT"] = str(self.config.root)
        env["THEOREM_SEARCH_PRIMARY"] = self.config.search.theorem_primary_endpoint
        env["THEOREM_SEARCH_FALLBACK"] = self.config.search.theorem_fallback_endpoint
        env["ARXIV_API_ENDPOINT"] = self.config.search.arxiv_api_endpoint
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    @staticmethod
    def _signal_process_group(
        process: asyncio.subprocess.Process,
        signal_number: signal.Signals,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            pass

    async def _wait_or_kill(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()
