from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from provider_config import apply_provider_config, extend_catalog


@dataclass(frozen=True)
class ModelConfig:
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class CodexConfig:
    binary: str
    provider: str
    base_url: str
    api_key_env: str
    history_persistence: str
    model_catalog: Path
    model_context_window: int
    model_auto_compact_token_limit: int
    use_docker_boundary: bool


@dataclass(frozen=True)
class SearchConfig:
    theorem_primary_endpoint: str
    theorem_fallback_endpoint: str
    arxiv_api_endpoint: str
    timeout_seconds: int


@dataclass(frozen=True)
class AppConfig:
    """Contract-first Planner transport configuration.

    The old Proof-DAG planner fields were intentionally removed.  MathLab's product
    lifecycle, repair policy and storage live in the top-level vNext settings; this
    object only configures the live Codex transport and Contract roles.
    """

    root: Path
    name: str
    runtime_dir: Path
    role_call_timeout_seconds: int
    codex: CodexConfig
    models: dict[str, ModelConfig]
    model_profiles: dict[str, dict[str, ModelConfig]]
    search: SearchConfig

    def model_for(self, role: str, profile: str = "high") -> ModelConfig:
        selected = self.model_profiles.get(profile) or self.model_profiles["high"]
        try:
            return selected[role]
        except KeyError:
            return self.models[role]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    root = config_path.parent
    with config_path.open("rb") as handle:
        data = apply_provider_config(tomllib.load(handle), "planner", root=root)

    app = data["application"]
    codex = data["codex"]
    search = data["search"]
    models = {name: ModelConfig(**value) for name, value in data["models"].items()}
    catalog_path = (root / str(codex["model_catalog"])).resolve()
    catalog_payload = extend_catalog(
        json.loads(catalog_path.read_text(encoding="utf-8")),
        {
            str(model.model) for model in models.values()
        }
        | {
            str(spec["model"])
            for table in (data.get("model_profiles") or {}).values()
            for spec in table.values()
        },
        root=root,
    )
    if os.environ.get("MATHLAB_ALLOW_UNLISTED_MODELS", "0") == "1":
        generated_catalog = (
            Path(os.environ.get("MATHLAB_DATA_ROOT", "~/.local/share/mathlab")).expanduser()
            / "runtime" / "planner" / "model_catalog.json"
        ).resolve()
        generated_catalog.parent.mkdir(parents=True, exist_ok=True)
        generated_catalog.write_text(json.dumps(catalog_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        catalog_path = generated_catalog
    catalog_models = {str(item["slug"]) for item in catalog_payload.get("models", [])}
    context_window = int(codex["model_context_window"])
    compact_limit = int(codex["model_auto_compact_token_limit"])
    if not 0 < compact_limit < context_window:
        raise ValueError(
            "Planner model_auto_compact_token_limit 必须小于 model_context_window"
        )
    required = {
        "contractor",
        "contract_integrator",
        "contract_fidelity_auditor",
        "contract_completeness_auditor",
        "contract_map_auditor",
    }
    missing = sorted(required - set(models))
    if missing:
        raise ValueError(f"Contract-first Planner 模型配置不完整：{missing}")
    profiles = {
        profile: {role: ModelConfig(**value) for role, value in payload.items()}
        for profile, payload in data["model_profiles"].items()
    }
    unknown_models = sorted(
        {
            model.model
            for table in (models, *profiles.values())
            for model in table.values()
            if model.model not in catalog_models
        }
    )
    if unknown_models:
        raise ValueError("Planner 模型目录缺少：" + ", ".join(unknown_models))
    for profile in ("low", "medium", "high"):
        if profile not in profiles:
            raise ValueError(f"缺少 Planner model profile：{profile}")
        missing_profile = sorted(required - set(profiles[profile]))
        if missing_profile:
            raise ValueError(f"Planner profile {profile} 缺少角色：{missing_profile}")

    data_root = Path(
        os.environ.get("MATHLAB_DATA_ROOT", "~/.local/share/mathlab")
    ).expanduser().resolve()
    return AppConfig(
        root=root,
        name=str(app["name"]),
        runtime_dir=data_root / "runtime" / "planner",
        role_call_timeout_seconds=int(app.get("role_call_timeout_seconds", 1800)),
        codex=CodexConfig(
            binary=str(codex["binary"]),
            provider=str(codex["provider"]),
            base_url=str(codex["base_url"]),
            api_key_env=str(codex["api_key_env"]),
            history_persistence=str(codex["history_persistence"]),
            model_catalog=catalog_path,
            model_context_window=context_window,
            model_auto_compact_token_limit=compact_limit,
            use_docker_boundary=os.environ.get(
                "MATHLAB_CODEX_USE_DOCKER_BOUNDARY", "0"
            ).strip().lower() in {"1", "true", "yes", "on"},
        ),
        models=models,
        model_profiles=profiles,
        search=SearchConfig(
            theorem_primary_endpoint=str(search["theorem_primary_endpoint"]),
            theorem_fallback_endpoint=str(search["theorem_fallback_endpoint"]),
            arxiv_api_endpoint=str(search["arxiv_api_endpoint"]),
            timeout_seconds=int(search.get("timeout_seconds", 45)),
        ),
    )
