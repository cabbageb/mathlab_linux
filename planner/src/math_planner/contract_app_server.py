from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import shutil

from .config import AppConfig, ModelConfig


TextDelta = Callable[[str], Awaitable[None]]
_JSONL_STREAM_LIMIT = 16 * 1024 * 1024


class ContractAppServerError(RuntimeError):
    """表示 Contract Chat 的 app-server transport 未正常完成。"""


@dataclass(frozen=True)
class ContractAppServerTurnResult:
    """保存一轮 Contract Chat 的最终正文、thread、状态和 usage。"""

    text: str
    status: str
    thread_id: str
    turn_id: str
    usage: dict[str, int]
    error: str | None = None


@dataclass
class _ActiveTurn:
    """在通知到达期间累积一个活动 turn 的流式正文与终态。"""

    thread_id: str
    on_text_delta: TextDelta | None
    done: asyncio.Future[dict[str, Any]]
    turn_id: str = ""
    streamed_text: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class ContractAppServerClient:
    """用一个 stdio app-server 为 Contract Chat 提供真正的文本 delta。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._active: dict[str, _ActiveTurn] = {}
        self._loaded_threads: set[str] = set()
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._codex_home = self.config.runtime_dir / "codex_home"
        self._protocol_log = self.config.runtime_dir / "contract_chat_app_server.jsonl"
        self._stderr_log = self.config.runtime_dir / "contract_chat_app_server.stderr.log"
        self._stderr_tail = ""

    def _sandbox(self) -> str:
        """Docker 内由容器隔离，宿主直跑时继续使用工作区沙盒。"""
        return (
            "danger-full-access"
            if self.config.codex.use_docker_boundary
            else "workspace-write"
        )

    def _sandbox_policy(self, cwd: Path) -> dict[str, Any]:
        """按部署边界生成 app-server turn 的沙盒协议对象。"""
        if self.config.codex.use_docker_boundary:
            return {"type": "dangerFullAccess"}
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(cwd.resolve())],
            "networkAccess": False,
        }

    async def start(self) -> None:
        """single-flight 启动 app-server，并声明 experimental API 能力。"""
        async with self._start_lock:
            if self._healthy():
                return
            binary = shutil.which(self.config.codex.binary)
            if not binary:
                raise ContractAppServerError(f"找不到 Codex CLI：{self.config.codex.binary}")
            if not os.environ.get(self.config.codex.api_key_env):
                raise ContractAppServerError(
                    f"环境变量 {self.config.codex.api_key_env} 未设置或为空"
                )
            self._codex_home.mkdir(parents=True, exist_ok=True)
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.root,
                env=self._environment(),
                limit=_JSONL_STREAM_LIMIT,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "math_lab",
                            "title": "MathLab Contract Chat",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    require_started=False,
                )
                await self._notify("initialized", {})
            except Exception:
                await self.stop()
                raise

    async def stop(self) -> None:
        """终止 transport 并让所有等待中的请求显式失败。"""
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        tasks = [self._reader_task, self._stderr_task, *self._server_tasks]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._server_tasks.clear()
        self._loaded_threads.clear()
        self._fail_pending(ContractAppServerError("Contract Chat app-server 已停止"))

    async def start_thread(
        self,
        *,
        model: ModelConfig,
        cwd: Path,
        developer_instructions: str,
        ephemeral: bool,
    ) -> str:
        """为一段 Contract Chat 创建持久或临时的 provider thread。"""
        await self.start()
        result = await self._request(
            "thread/start",
            {
                "model": model.model,
                "modelProvider": self.config.codex.provider,
                "cwd": str(cwd.resolve()),
                "approvalPolicy": "never",
                "sandbox": self._sandbox(),
                "developerInstructions": developer_instructions,
                "serviceName": "math_lab",
                "ephemeral": ephemeral,
            },
        )
        thread_id = str((result.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise ContractAppServerError("thread/start 未返回 thread id")
        self._loaded_threads.add(thread_id)
        return thread_id

    async def resume_thread(
        self,
        thread_id: str,
        *,
        model: ModelConfig,
        cwd: Path,
        developer_instructions: str,
    ) -> None:
        """服务重启后恢复 Contract Chat 的原 provider thread。"""
        await self.start()
        if thread_id in self._loaded_threads:
            return
        await self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "model": model.model,
                "modelProvider": self.config.codex.provider,
                "cwd": str(cwd.resolve()),
                "approvalPolicy": "never",
                "sandbox": self._sandbox(),
                "developerInstructions": developer_instructions,
            },
        )
        self._loaded_threads.add(thread_id)

    async def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        model: ModelConfig,
        cwd: Path,
        on_text_delta: TextDelta | None,
    ) -> ContractAppServerTurnResult:
        """启动一轮模型调用，并把 agentMessage delta 原样交给上层投影器。"""
        if thread_id in self._active:
            raise ContractAppServerError(f"thread {thread_id} 已有活动 turn")
        loop = asyncio.get_running_loop()
        active = _ActiveTurn(
            thread_id=thread_id,
            on_text_delta=on_text_delta,
            done=loop.create_future(),
        )
        self._active[thread_id] = active
        try:
            started = await self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                    "model": model.model,
                    "effort": model.reasoning_effort,
                    "cwd": str(cwd.resolve()),
                    "approvalPolicy": "never",
                    "sandboxPolicy": self._sandbox_policy(cwd),
                    "summary": "concise",
                },
            )
            active.turn_id = str((started.get("turn") or {}).get("id") or "")
            completed = await asyncio.wait_for(
                active.done, timeout=self.config.role_call_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise ContractAppServerError(
                f"Contract Chat 超过 {self.config.role_call_timeout_seconds} 秒"
            ) from exc
        finally:
            self._active.pop(thread_id, None)
        turn = completed.get("turn") or {}
        status = str(turn.get("status") or "failed")
        error = turn.get("error")
        return ContractAppServerTurnResult(
            text=self._final_text(active),
            status=status,
            thread_id=thread_id,
            turn_id=active.turn_id,
            usage=active.usage,
            error=json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else (str(error) if error else None),
        )

    async def _read_stdout(self) -> None:
        """解析 JSON-RPC 响应、通知和拒绝类服务端请求。"""
        assert self._process and self._process.stdout
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._log_protocol("server", message)
                if "method" in message and "id" in message:
                    task = asyncio.create_task(self._handle_server_request(message))
                    self._server_tasks.add(task)
                    task.add_done_callback(self._server_tasks.discard)
                elif "id" in message:
                    request_id = int(message["id"])
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        if message.get("error") is not None:
                            future.set_exception(
                                ContractAppServerError(json.dumps(message["error"], ensure_ascii=False))
                            )
                        else:
                            future.set_result(message.get("result") or {})
                elif "method" in message:
                    await self._handle_notification(message)
        except asyncio.CancelledError:
            raise
        finally:
            if self._process is not None:
                self._fail_pending(
                    ContractAppServerError(
                        "Contract Chat app-server 连接已关闭"
                        + (f"：{self._stderr_tail}" if self._stderr_tail else "")
                    )
                )

    async def _read_stderr(self) -> None:
        """持续消费 stderr，防止子进程因管道填满而阻塞。"""
        assert self._process and self._process.stderr
        while line := await self._process.stderr.readline():
            decoded = line.decode("utf-8", errors="replace")
            self._stderr_tail = (self._stderr_tail + decoded)[-8000:]
            self._stderr_log.parent.mkdir(parents=True, exist_ok=True)
            with self._stderr_log.open("a", encoding="utf-8") as handle:
                handle.write(decoded)

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        active = self._active.get(thread_id)
        if method == "item/agentMessage/delta" and active:
            delta = str(params.get("delta") or "")
            active.streamed_text += delta
            if delta and active.on_text_delta:
                await active.on_text_delta(delta)
        elif method == "item/completed" and active:
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                active.messages.append(
                    {"phase": str(item.get("phase") or ""), "text": str(item.get("text") or "")}
                )
        elif method == "thread/tokenUsage/updated" and active:
            usage = (params.get("tokenUsage") or {}).get("last") or {}
            active.usage = {
                "input_tokens": int(usage.get("inputTokens") or 0),
                "cached_input_tokens": int(usage.get("cachedInputTokens") or 0),
                "output_tokens": int(usage.get("outputTokens") or 0),
                "reasoning_output_tokens": int(usage.get("reasoningOutputTokens") or 0),
                "total_tokens": int(usage.get("totalTokens") or 0),
            }
        elif method == "turn/completed":
            candidate = active or self._turn_for_id(str((params.get("turn") or {}).get("id") or ""))
            if candidate and not candidate.done.done():
                candidate.done.set_result(params)
        elif method == "error" and active and not params.get("willRetry") and not active.done.done():
            active.done.set_result(
                {"turn": {"id": active.turn_id, "status": "failed", "error": params.get("error") or params}}
            )

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """Contract Chat 无动态工具；所有权限升级和用户输入请求均拒绝。"""
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            await self._respond(
                request_id,
                error={"code": -32000, "message": f"Contract Chat 不支持服务端请求：{method}"},
            )
            return
        await self._respond(request_id, result=result)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        require_started: bool = True,
    ) -> dict[str, Any]:
        if require_started:
            await self.start()
        elif self._process is None:
            raise ContractAppServerError("Contract Chat app-server 未启动")
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params})
        return await future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _respond(
        self,
        request_id: Any,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ContractAppServerError("Contract Chat app-server 未运行")
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._log_protocol("client", message)
            process.stdin.write(encoded)
            await process.stdin.drain()

    def _log_protocol(self, direction: str, message: dict[str, Any]) -> None:
        self._protocol_log.parent.mkdir(parents=True, exist_ok=True)
        with self._protocol_log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"direction": direction, "message": message}, ensure_ascii=False) + "\n"
            )

    def _healthy(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    def _turn_for_id(self, turn_id: str) -> _ActiveTurn | None:
        return next((item for item in self._active.values() if item.turn_id == turn_id), None)

    @staticmethod
    def _final_text(turn: _ActiveTurn) -> str:
        finals = [item["text"] for item in turn.messages if item.get("phase") == "final_answer" and item.get("text")]
        if finals:
            return finals[-1]
        messages = [item["text"] for item in turn.messages if item.get("text")]
        return messages[-1] if messages else turn.streamed_text

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for turn in self._active.values():
            if not turn.done.done():
                turn.done.set_exception(error)

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self._codex_home)
        env["MATH_PLANNER_ROOT"] = str(self.config.root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env
