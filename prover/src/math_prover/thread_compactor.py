from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import json
import os
import shutil

from .config import AppConfig, ModelConfig
from .codex_permissions import (
    ensure_permission_profiles_supported,
    managed_runtime_path,
    managed_runtime_workspace,
    permission_profile_name,
)


class ThreadCompactionError(RuntimeError):
    """表示 Codex app-server 未能完成 thread 压缩。"""


class CodexThreadCompactor:
    """复用 Prover CODEX_HOME，通过 app-server 对持久 thread 做显式压缩。"""

    def __init__(self, config: AppConfig, codex_home: Path) -> None:
        self.config = config
        self.codex_home = codex_home
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.loaded_threads: set[str] = set()
        self.request_id = 0
        self.write_lock = asyncio.Lock()
        self.stderr_tail = ""

    async def start(self) -> None:
        """启动 stdio app-server，并完成协议初始化。"""
        if self.process is not None and self.process.returncode is None:
            return
        binary = shutil.which(self.config.codex.binary)
        if not binary:
            raise ThreadCompactionError(f"找不到 Codex CLI：{self.config.codex.binary}")
        try:
            ensure_permission_profiles_supported(self.config.codex.binary)
        except RuntimeError as exc:
            raise ThreadCompactionError(str(exc)) from exc
        self.process = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.root,
            env=self._environment(),
        )
        self.reader_task = asyncio.create_task(self._read_stdout())
        self.stderr_task = asyncio.create_task(self._read_stderr())
        await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "math_prover",
                    "title": "Hierarchical Mathematical Prover",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._notify("initialized", {})

    async def stop(self) -> None:
        """终止 app-server，并收束仍在等待的协议请求。"""
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.reader_task, self.stderr_task) if task is not None),
            return_exceptions=True,
        )
        self.reader_task = None
        self.stderr_task = None
        self.loaded_threads.clear()
        error = ThreadCompactionError("app-server 已停止")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    async def compact(
        self,
        thread_id: str,
        *,
        model: ModelConfig,
        cwd: Path,
        instructions: str,
    ) -> None:
        """恢复指定持久 thread，并请求 Codex 压缩较早上下文。"""
        workspace = managed_runtime_workspace(
            self.config, cwd, "压缩角色工作目录"
        )
        await self.start()
        if thread_id not in self.loaded_threads:
            network_access = (
                "<!-- controller-network-access: enabled -->" in instructions
            )
            sandbox = (
                {"sandbox": "danger-full-access"}
                if self.config.codex.use_docker_boundary
                else {
                    "permissions": permission_profile_name(
                        network_access=network_access
                    ),
                    "runtimeWorkspaceRoots": [str(workspace)],
                }
            )
            await self._request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "model": model.model,
                    "modelProvider": self.config.codex.provider,
                    "cwd": str(workspace),
                    "approvalPolicy": "never",
                    **sandbox,
                    "developerInstructions": instructions,
                },
            )
            self.loaded_threads.add(thread_id)
        await self._request("thread/compact/start", {"threadId": thread_id})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送一个 JSON-RPC 请求，并等待相同编号的响应。"""
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ThreadCompactionError("app-server 尚未运行")
        self.request_id += 1
        request_id = self.request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=30)
        finally:
            self.pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """发送无需响应的 JSON-RPC 通知。"""
        await self._write({"method": method, "params": params})

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise ThreadCompactionError("app-server stdin 不可用")
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self.write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        """消费 app-server 响应，并唤醒对应请求。"""
        process = self.process
        if process is None or process.stdout is None:
            return
        while line := await process.stdout.readline():
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            request_id = message.get("id")
            if request_id is None:
                continue
            future = self.pending.get(int(request_id))
            if future is None or future.done():
                continue
            if "error" in message:
                future.set_exception(
                    ThreadCompactionError(json.dumps(message["error"], ensure_ascii=False))
                )
            else:
                future.set_result(message.get("result") or {})
        if self.process is not None and self.process.returncode is None:
            return
        error = ThreadCompactionError(
            "app-server 意外退出" + (f"：{self.stderr_tail}" if self.stderr_tail else "")
        )
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)

    async def _read_stderr(self) -> None:
        """保留 app-server stderr 尾部，供协议失败时诊断。"""
        process = self.process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            self.stderr_tail = (
                self.stderr_tail + line.decode("utf-8", errors="replace")
            )[-4000:]

    def _environment(self) -> dict[str, str]:
        """构造与 exec 客户端一致、但不暴露密钥给模型 shell 的环境。"""
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.codex_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["THEOREM_SEARCH_PRIMARY"] = self.config.search.theorem_primary_endpoint
        env["THEOREM_SEARCH_FALLBACK"] = self.config.search.theorem_fallback_endpoint
        env["ARXIV_API_ENDPOINT"] = self.config.search.arxiv_api_endpoint
        return env
