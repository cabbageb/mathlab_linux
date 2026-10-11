from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import sys
import time


_INTERNAL_LOG = re.compile(r"(?:^|[/\\\s'\"])(?:stream\.jsonl|stderr\.log)(?:$|[\s'\"])")
_SCALE_ASSIGNMENT = re.compile(
    r"(?im)\b(?:n|max_n|limit|size|count|num_items|samples|trials)\s*=\s*([0-9][0-9_]*)\b"
)
_SCALE_FLAG = re.compile(
    r"(?i)--(?:n|max-n|limit|size|count|num-items|samples|trials)\s+([0-9][0-9_]*)\b"
)
_SCALE_RANGE = re.compile(r"(?i)\brange\(\s*([0-9][0-9_]*)\b")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.])(/[^\s'\";|&<>]+)")
_ISOLATED_TEMP_PATH = re.compile(r"(?<![\w.])/(?:tmp|var/tmp)(?:/|\b)")
_BARE_PYTHON = re.compile(r"(?<![\w./-])python(?=\s)")
_NON_STDLIB_IMPORT = re.compile(
    r"(?im)^\s*(?:from|import)\s+(sympy|pulp|cvxopt)\b"
)
_PYTHON_SCRIPT = re.compile(
    r"(?<![\w./-])python3\s+(?!-)([^\s'\";|&<>]+\.py)\b"
)


def _deny(reason: str) -> dict[str, Any]:
    """生成 Codex PreToolUse 的确定性拒绝结构。"""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _non_stdlib_module(command: str, cwd: str | None) -> str | None:
    """检查内联或当前 workspace 脚本是否依赖未承诺的第三方计算包。"""
    sources = [command]
    if cwd:
        root = Path(cwd).resolve()
        for raw_path in _PYTHON_SCRIPT.findall(command):
            candidate = (root / raw_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file() and candidate.stat().st_size <= 1_048_576:
                sources.append(candidate.read_text(encoding="utf-8", errors="replace"))
    for source in sources:
        match = _NON_STDLIB_IMPORT.search(source)
        if match:
            return match.group(1)
    return None


def _command_policy_reason(command: str, cwd: str | None = None) -> str | None:
    """拒绝内部日志扫描和明显超过公共 compute guide 的枚举规模。"""
    if _INTERNAL_LOG.search(command):
        return "不要读取内部 stream.jsonl 或 stderr.log；请直接完成当前数学任务。"
    if "bounded_compute.py" in command:
        values = [
            int(value.replace("_", ""))
            for pattern in (_SCALE_ASSIGNMENT, _SCALE_FLAG, _SCALE_RANGE)
            for value in pattern.findall(command)
        ]
        if values and max(values) > 5_000:
            return (
                "有限枚举首轮规模不得超过 5,000。请把 N/limit/size/count "
                "等规模参数降到 5,000 以下，并先完成小规模测时。"
            )
    if _ISOLATED_TEMP_PATH.search(command):
        return (
            "受限 shell 的 /tmp 与 /var/tmp 在不同调用之间不共享。请把临时脚本"
            "保存在当前 Agent workspace，并始终使用相对路径。"
        )
    if _BARE_PYTHON.search(command):
        return "受限 shell 只保证 python3；请把命令中的裸 python 改为 python3。"
    unsupported = _non_stdlib_module(command, cwd)
    if unsupported is not None:
        return (
            f"bounded compute 不保证第三方模块 {unsupported}。请只使用 Python "
            "标准库（例如 fractions/decimal）完成小规模精确计算。"
        )
    for raw_path in _ABSOLUTE_PATH.findall(command):
        candidate = raw_path.rstrip(",:)]}")
        if not any(marker in candidate for marker in ("/research_runs/", "/runtime/agents/")):
            continue
        if not Path(candidate).exists():
            return (
                "命令包含不存在的 MathLab runtime 绝对路径。请改用当前 Agent cwd "
                "内的相对路径，不要手工复制或拼接绝对路径。"
            )
    return None


def _model_arguments_once(payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """尝试从 transcript 尾部读取本次 Bash function call 的原始参数。"""
    transcript = payload.get("transcript_path")
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(transcript, str) or not isinstance(tool_use_id, str):
        return False, {}
    path = Path(transcript)
    if not path.is_file():
        return False, {}
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - 262_144))
        data = handle.read()
    if size > 262_144 and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    for raw_line in reversed(data.splitlines()):
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        item = event.get("payload") or {}
        if (
            event.get("type") != "response_item"
            or item.get("type") != "function_call"
            or item.get("call_id") != tool_use_id
        ):
            continue
        try:
            arguments = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            return True, {}
        return True, dict(arguments) if isinstance(arguments, dict) else {}
    return False, {}


def _model_arguments(payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """短暂等待 function call 落盘，避免高并发 transcript 写入竞争。"""
    for _ in range(100):
        found, arguments = _model_arguments_once(payload)
        if found:
            return True, arguments
        time.sleep(0.01)
    return False, {}


def guard_shell_workspace(payload: dict[str, Any]) -> dict[str, Any]:
    """在 Bash 执行前拒绝偏离 Controller 会话 cwd 的模型 workdir。"""
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    cwd = payload.get("cwd")
    if payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return {}
    policy_reason = _command_policy_reason(command, cwd if isinstance(cwd, str) else None)
    if policy_reason is not None:
        return _deny(policy_reason)
    if not isinstance(cwd, str) or not cwd:
        return _deny("MathLab Controller cwd is missing.")
    found, arguments = _model_arguments(payload)
    if not found:
        arguments = dict(tool_input)
    if "tools/fetch_arxiv.py" in command:
        timeout_ms = arguments.get("timeout_ms")
        if not isinstance(timeout_ms, (int, float)) or timeout_ms < 180_000:
            return _deny(
                "fetch_arxiv 需要下载、解包并解析 TeX/PDF。请用相同 command "
                "重试，并设置 timeout_ms=180000 或更大。"
            )
    requested = arguments.get("workdir")
    if (
        requested is None or Path(requested).resolve() == Path(cwd).resolve()
    ):
        return {}
    return _deny(
        "Controller 已固定当前 Agent 工作目录，且本次 workdir 未通过核对。"
        "请用完全相同的 command 立即重试，并省略 workdir 参数。"
    )


def main() -> int:
    """读取一个 PreToolUse JSON，并输出 Bash 工作目录门禁结论。"""
    payload = json.load(sys.stdin)
    result = guard_shell_workspace(payload)
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
