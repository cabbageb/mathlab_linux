from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

from .config import AppConfig


OFFLINE_PERMISSION_PROFILE = "math-prover-offline"
LITERATURE_PERMISSION_PROFILE = "math-prover-literature"
MINIMUM_PERMISSION_PROFILE_VERSION = (0, 138, 0)
PROJECT_ROOT_MARKER = ".math_prover_root"


def ensure_permission_profiles_supported(binary_name: str) -> None:
    """启动模型前核验 Codex 版本支持原生权限档案，否则关闭式失败。"""
    binary = shutil.which(binary_name)
    if not binary:
        raise RuntimeError(f"找不到 Codex CLI：{binary_name}")
    completed = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    version_text = (completed.stdout or completed.stderr).strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if (
        completed.returncode != 0
        or match is None
        or "codex" not in version_text.lower()
    ):
        raise RuntimeError(f"无法核验 Codex 权限档案版本：{version_text or '无输出'}")
    version = tuple(int(value) for value in match.groups())
    if version < MINIMUM_PERMISSION_PROFILE_VERSION:
        required = ".".join(str(value) for value in MINIMUM_PERMISSION_PROFILE_VERSION)
        actual = ".".join(str(value) for value in version)
        raise RuntimeError(
            f"Codex {actual} 不支持 Math Prover 硬读取边界，至少需要 {required}"
        )


def permission_profile_name(*, network_access: bool) -> str:
    """按 Controller 的网络授权选择离线或文献权限档案。"""
    return (
        LITERATURE_PERMISSION_PROFILE
        if network_access
        else OFFLINE_PERMISSION_PROFILE
    )


def render_permission_profiles(config: AppConfig) -> str:
    """生成 Codex 原生权限档案，以单个 Agent cwd 作为唯一业务读写根。"""
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":workspace_roots": {".": "write"},
        str(_codex_install_root(config)): "read",
        str(Path(sys.prefix).resolve()): "read",
    }
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver.is_file() and resolver != Path("/etc/resolv.conf"):
        filesystem[str(resolver)] = "read"
    offline_network = {
        "localhost": "allow",
        "127.0.0.1": "allow",
    }
    literature_network = dict(offline_network)
    for endpoint in (
        config.search.theorem_primary_endpoint,
        config.search.theorem_fallback_endpoint,
        config.search.arxiv_api_endpoint,
        "https://arxiv.org",
        "https://export.arxiv.org",
    ):
        hostname = (urlparse(endpoint).hostname or "").strip().lower()
        if hostname:
            literature_network[hostname] = "allow"
    return (
        f"default_permissions = {json.dumps(OFFLINE_PERMISSION_PROFILE)}\n\n"
        f"[permissions.{OFFLINE_PERMISSION_PROFILE}]\n"
        'description = "Math Prover Agent：只读最小系统依赖，只读写本 Agent 工作区。"\n'
        f"filesystem = {_toml_inline_table(filesystem)}\n"
        "network = "
        f"{_toml_inline_table({'enabled': True, 'domains': offline_network})}\n\n"
        f"[permissions.{LITERATURE_PERMISSION_PROFILE}]\n"
        'description = "Math Prover 文献 Agent：在离线边界上增加受限文献域名。"\n'
        f"extends = {json.dumps(OFFLINE_PERMISSION_PROFILE)}\n"
        "network = "
        f"{_toml_inline_table({'enabled': True, 'domains': literature_network})}\n"
    )


def write_generated_codex_config(path: Path, content: str) -> None:
    """原子替换 Controller 所有的 Codex 配置，并保留同目录认证与会话文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def ensure_agent_project_root(workspace: Path) -> Path:
    """原子创建 Agent 独立项目根标记，阻止 Codex 向父目录收集 AGENTS.md。"""
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / PROJECT_ROOT_MARKER
    if marker.is_file():
        return marker
    if marker.exists():
        raise RuntimeError(f"Agent 项目根标记不是普通文件：{marker}")
    temporary = workspace / f"{PROJECT_ROOT_MARKER}.{uuid.uuid4().hex}.tmp"
    temporary.write_text("", encoding="utf-8")
    os.replace(temporary, marker)
    return marker


def managed_runtime_path(config: AppConfig, path: Path, label: str) -> Path:
    """只接受由 MATHLAB_DATA_ROOT 派生的 Prover runtime 工作路径。"""
    resolved = path.resolve()
    root = config.runtime_dir.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"{label}必须位于 MathLab 管理的 Prover runtime 内")
    return resolved


def managed_runtime_workspace(config: AppConfig, path: Path, label: str) -> Path:
    """解析并建立稳定 Agent cwd，拒绝文件路径和 runtime 外路径。"""
    workspace = managed_runtime_path(config, path, label)
    if workspace.exists() and not workspace.is_dir():
        raise RuntimeError(f"{label}必须是目录：{workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    ensure_agent_project_root(workspace)
    return workspace


def _codex_install_root(config: AppConfig) -> Path:
    """定位 Codex 包根目录，供 bundled bubblewrap 只读执行自身辅助程序。"""
    binary = shutil.which(config.codex.binary)
    if not binary:
        raise RuntimeError(f"找不到 Codex CLI：{config.codex.binary}")
    resolved = Path(binary).resolve()
    if resolved.name == "codex.js" and resolved.parent.name == "bin":
        return resolved.parent.parent
    return resolved.parent


def _toml_inline_table(values: dict[str, object]) -> str:
    """稳定渲染字符串键值的 TOML inline table，避免路径和域名转义歧义。"""
    body = ", ".join(
        f"{json.dumps(key)} = {_toml_value(value)}"
        for key, value in sorted(values.items())
    )
    return "{ " + body + " }"


def _toml_value(value: object) -> str:
    """渲染权限档案所需的字符串、布尔值和嵌套表。"""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return _toml_inline_table(value)
    raise TypeError(f"不支持的权限 TOML 值：{value!r}")
