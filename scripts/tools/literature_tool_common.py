from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import socket


def load_access(config_path: str = "input/literature_access.json") -> tuple[dict[str, Any], Path]:
    """读取 Controller 写入的单次回环能力，并核验产物根位于当前 Agent cwd。"""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    operation_root = Path(str(config["operation_root"])).resolve()
    cwd = Path.cwd().resolve()
    if not operation_root.is_relative_to(cwd):
        raise RuntimeError("literature operation root 越出当前 Agent 工作区")
    return config, operation_root


def request(action: str, payload: dict[str, Any], config_path: str = "input/literature_access.json") -> tuple[dict[str, Any], Path]:
    """向 Controller 回环服务发送一条 JSON 动作并返回业务结果。"""
    config, operation_root = load_access(config_path)
    host, port_text = str(config["endpoint"]).rsplit(":", 1)
    message = {"token": config["token"], "action": action, **payload}
    with socket.create_connection((host, int(port_text)), timeout=180) as connection:
        stream = connection.makefile("rwb")
        stream.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        stream.flush()
        response = json.loads(stream.readline().decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "literature access failed"))
    return dict(response["result"]), operation_root


def write_json(path: Path, payload: object) -> None:
    """原子写入工具证据 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
