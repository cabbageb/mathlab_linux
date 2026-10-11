"""读取 MathLab Linux 发行版的 provider 与模型覆盖配置。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib
from typing import Any


def load_provider_config(root: Path) -> dict[str, Any]:
    """加载用户可编辑的 provider TOML；缺少文件时返回空覆盖。"""
    configured = os.environ.get("MATHLAB_PROVIDER_CONFIG", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        candidates = (root / "config" / "provider.toml", root.parent / "config" / "provider.toml")
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _merge_model(existing: dict[str, Any], override: Any) -> dict[str, Any]:
    value = dict(existing)
    if isinstance(override, str):
        value["model"] = override
    elif isinstance(override, dict):
        value.update(override)
    return value


def apply_provider_config(raw: dict[str, Any], component: str, *, root: Path | None = None) -> dict[str, Any]:
    """把 provider.toml 的通用配置叠加到 planner/prover 原始 TOML。"""
    data = deepcopy(raw)
    overrides = load_provider_config(root or Path.cwd())
    provider = dict(overrides.get("provider") or {})
    wire_api = str(provider.get("wire_api", "responses")).strip().lower()
    if wire_api != "responses":
        raise ValueError("MathLab Linux 当前只支持 Responses provider；Chat-only provider 暂不支持")

    codex = data.setdefault("codex", {})
    for key in ("provider", "base_url", "api_key_env", "model_context_window", "model_auto_compact_token_limit"):
        if key in provider:
            codex[key] = provider[key]

    default_model = provider.get("default_model")
    default_effort = provider.get("default_reasoning_effort")
    if default_model:
        for table in (data.get("models") or {}, *(data.get("model_profiles") or {}).values()):
            for role, spec in list(table.items()):
                updated = _merge_model(spec, str(default_model))
                if default_effort:
                    updated["reasoning_effort"] = str(default_effort)
                table[role] = updated

    profiles = overrides.get("profiles") or {}
    for profile, profile_override in profiles.items():
        profile_names = [profile]
        if component == "prover":
            profile_names = [{"low": "gpt_low", "medium": "gpt_medium", "high": "gpt_high"}.get(profile, profile)]
        tables = [
            data.setdefault("model_profiles", {}).setdefault(name, {})
            for name in profile_names
        ]
        if isinstance(profile_override, dict):
            model = profile_override.get("model")
            effort = profile_override.get("reasoning_effort")
            if model:
                for table in tables:
                    for role, spec in list(table.items()):
                        updated = _merge_model(spec, str(model))
                        if effort:
                            updated["reasoning_effort"] = str(effort)
                        table[role] = updated

    component_models = (overrides.get("models") or {}).get(component) or {}
    for role, override in component_models.items():
        if role in data.get("models", {}):
            data["models"][role] = _merge_model(data["models"][role], override)

    component_profiles = (overrides.get("model_profiles") or {}).get(component) or {}
    for profile, role_overrides in component_profiles.items():
        table = data.setdefault("model_profiles", {}).setdefault(profile, {})
        for role, override in (role_overrides or {}).items():
            if role in table:
                table[role] = _merge_model(table[role], override)

    return data


def extend_catalog(
    payload: dict[str, Any], configured_models: set[str], *, root: Path | None = None
) -> dict[str, Any]:
    """为本地自定义模型补充 Codex catalog 条目，避免要求改源码。"""
    result = deepcopy(payload)
    known = {str(item.get("slug")) for item in result.get("models", [])}
    if os.environ.get("MATHLAB_ALLOW_UNLISTED_MODELS", "0") != "1":
        return result
    provider = load_provider_config(root or Path.cwd()).get("provider") or {}
    context_window = int(provider.get("model_context_window", 1_000_000))
    efforts = provider.get("supported_reasoning_levels") or ["low", "medium", "high", "xhigh", "max"]
    for slug in sorted(configured_models - known):
        result.setdefault("models", []).append(
            {
                "slug": slug,
                "display_name": slug,
                "description": "User-configured Responses-compatible model.",
                "default_reasoning_level": str(efforts[-1]),
                "supported_reasoning_levels": [
                    {"effort": str(effort), "description": str(effort)} for effort in efforts
                ],
                "context_window": context_window,
                "effective_context_window_percent": 95,
                "supports_parallel_tool_calls": True,
                "input_modalities": ["text"],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": len(result["models"]) + 1,
                "support_verbosity": True,
                "supports_reasoning_summaries": False,
                "experimental_supported_tools": [],
                "truncation_policy": {"mode": "tokens", "limit": 10000},
            }
        )
    return result
