from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import hashlib
import json
import secrets


MAX_ARTIFACT_CHARS = 200_000


class ReviewerEvidenceAccessServer:
    """通过一次性回环 token 暴露当前 Run 白名单证据，并记录实际 list/open。"""

    def __init__(
        self,
        artifacts: list[dict[str, Any]],
        audit_path: Path,
    ) -> None:
        self.artifacts = {str(item["artifact_id"]): dict(item) for item in artifacts}
        self.audit_path = audit_path.resolve()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.token = "reviewer-" + secrets.token_urlsafe(32)
        self.endpoint = ""
        self.server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> ReviewerEvidenceAccessServer:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        host, port = self.server.sockets[0].getsockname()[:2]
        self.endpoint = f"{host}:{port}"
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def client_payload(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "token": self.token}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            payload = json.loads((await reader.readline()).decode("utf-8"))
            response = self.dispatch(payload)
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只允许列举或打开清单内文件，绝不接受调用者传入路径。"""
        if payload.get("token") != self.token:
            raise PermissionError("Reviewer evidence token 无效")
        action = str(payload.get("action") or "")
        if action == "list":
            kind = str(payload.get("kind") or "")
            items = [
                {
                    "artifact_id": item["artifact_id"],
                    "kind": item["kind"],
                    "title": item["title"],
                    "sha256": item["sha256"],
                }
                for item in self.artifacts.values()
                if not kind or item["kind"] == kind
            ]
            response = {"ok": True, "artifacts": items}
            self._audit("list", kind or "*", True)
            return response
        if action == "open":
            artifact_id = str(payload.get("artifact_id") or "")
            item = self.artifacts.get(artifact_id)
            if item is None:
                self._audit("open", artifact_id, False)
                raise FileNotFoundError("Artifact ID 不在当前 Reviewer 白名单")
            path = Path(str(item["path"])).resolve()
            if not path.is_file():
                self._audit("open", artifact_id, False)
                raise FileNotFoundError("白名单 Artifact 已不存在")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                self._audit("open", artifact_id, False)
                raise RuntimeError("Artifact SHA 已变化")
            text = path.read_text(encoding="utf-8")
            truncated = len(text) > MAX_ARTIFACT_CHARS
            self._audit("open", artifact_id, True)
            return {
                "ok": True,
                "artifact_id": artifact_id,
                "kind": item["kind"],
                "title": item["title"],
                "sha256": digest,
                "text": text[:MAX_ARTIFACT_CHARS],
                "truncated": truncated,
            }
        raise ValueError("Reviewer evidence action 只能是 list/open")

    def _audit(self, action: str, target: str, ok: bool) -> None:
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                {"action": action, "target": target, "ok": ok},
                ensure_ascii=False,
            ) + "\n")


def read_reviewer_evidence_audit(path: Path) -> dict[str, Any]:
    """汇总 Reviewer 本轮真实 evidence list/open，用于持久化而非信任自报。"""
    rows = []
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "listed": any(item.get("action") == "list" and item.get("ok") for item in rows),
        "opened_artifact_ids": [
            str(item.get("target")) for item in rows
            if item.get("action") == "open" and item.get("ok")
        ],
        "actions": rows,
    }
