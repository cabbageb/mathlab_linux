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
    model_auto_compact_token_limit: int
    use_docker_boundary: bool


@dataclass(frozen=True)
class SearchConfig:
    theorem_primary_endpoint: str
    theorem_fallback_endpoint: str
    arxiv_api_endpoint: str
    max_results: int
    timeout_seconds: int
    max_literature_theorem_searches: int
    max_literature_arxiv_searches: int
    max_literature_abstracts: int
    max_literature_papers: int


@dataclass(frozen=True)
class PublicationLiteratureConfig:
    enabled: bool
    theorem_searches: int
    arxiv_searches: int
    abstract_candidates: int
    fulltext_papers: int
    abstract_keep: int


@dataclass(frozen=True)
class AppConfig:
    """Transport configuration for the Dynamic Research Graph Prover.

    Runtime scheduling, budget and repair limits are owned by VNextSettings.  The old
    Target/Theorem/Lemma/Closure budget hierarchy is deliberately absent.
    """

    root: Path
    runtime_dir: Path
    role_call_timeout_seconds: int
    paper_writer_timeout_seconds: int
    paper_writer_warning_remaining_seconds: int
    codex: CodexConfig
    models: dict[str, ModelConfig]
    model_profiles: dict[str, dict[str, ModelConfig]]
    search: SearchConfig
    publication_literature: PublicationLiteratureConfig


REQUIRED_MODEL_KEYS = {
    "research_literature",
    "research_construction",
    "research_general_proof",
    "research_cross_domain",
    "research_node_supervisor",
    "research_architecture_reviewer",
    "research_claim_verifier",
    "paper_writer",
    "paper_auditor",
    "public_summarizer",
}


def _validate_model_table(
    name: str,
    models: dict[str, ModelConfig],
    catalog: dict[str, set[str]],
) -> None:
    """核验一套角色模型表完整，并确保模型与推理强度均受 catalog 支持。"""
    missing = sorted(REQUIRED_MODEL_KEYS - set(models))
    extra = sorted(set(models) - REQUIRED_MODEL_KEYS)
    if missing or extra:
        raise ValueError(f"Dynamic Prover 模型配置 `{name}` 字段不匹配：missing={missing}, extra={extra}")
    for role, model in models.items():
        efforts = catalog.get(model.model)
        if efforts is None:
            raise ValueError(f"Dynamic Prover 模型配置 `{name}.{role}` 使用未知模型：{model.model}")
        if model.reasoning_effort not in efforts:
            raise ValueError(
                f"Dynamic Prover 模型配置 `{name}.{role}` 的 reasoning effort 不受支持："
                f"{model.model}/{model.reasoning_effort}"
            )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    root = config_path.parent
    with config_path.open("rb") as handle:
        data = apply_provider_config(tomllib.load(handle), "prover", root=root)
    app = data["application"]
    codex = data["codex"]
    search = data["search"]
    publication_literature = data.get("publication_literature") or {}
    models = {name: ModelConfig(**value) for name, value in data["models"].items()}
    model_profiles = {
        profile: {name: ModelConfig(**value) for name, value in table.items()}
        for profile, table in (data.get("model_profiles") or {}).items()
    }
    catalog_path = (root / codex["model_catalog"]).resolve()
    configured_models = {str(model.model) for model in models.values()} | {
        str(spec["model"])
        for table in (data.get("model_profiles") or {}).values()
        for spec in table.values()
    }
    catalog_payload = extend_catalog(
        json.loads(catalog_path.read_text(encoding="utf-8")), configured_models, root=root
    )
    if os.environ.get("MATHLAB_ALLOW_UNLISTED_MODELS", "0") == "1":
        generated_catalog = (
            Path(os.environ.get("MATHLAB_DATA_ROOT", "~/.local/share/mathlab")).expanduser()
            / "runtime" / "prover" / "model_catalog.json"
        ).resolve()
        generated_catalog.parent.mkdir(parents=True, exist_ok=True)
        generated_catalog.write_text(json.dumps(catalog_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        catalog_path = generated_catalog
    compact_limit = int(codex["model_auto_compact_token_limit"])
    effective_windows = {
        str(item["slug"]): (
            int(item.get("context_window") or 0)
            * int(item.get("effective_context_window_percent") or 100)
            // 100
        )
        for item in catalog_payload.get("models", [])
    }
    catalog = {
        str(item["slug"]): {
            str(level["effort"]) for level in item.get("supported_reasoning_levels", [])
        }
        for item in catalog_payload.get("models", [])
    }
    _validate_model_table("models", models, catalog)
    for profile, table in model_profiles.items():
        _validate_model_table(f"model_profiles.{profile}", table, catalog)
    configured_models = {
        model.model
        for table in (models, *model_profiles.values())
        for model in table.values()
    }
    too_small = sorted(
        model
        for model in configured_models
        if effective_windows.get(model, 0) <= compact_limit
    )
    if too_small:
        raise ValueError(
            "Prover model_auto_compact_token_limit 必须小于模型有效窗口："
            + ", ".join(too_small)
        )
    data_root = Path(
        os.environ.get("MATHLAB_DATA_ROOT", "~/.local/share/mathlab")
    ).expanduser().resolve()
    return AppConfig(
        root=root,
        runtime_dir=data_root / "runtime" / "prover",
        role_call_timeout_seconds=int(app.get("role_call_timeout_seconds", 1800)),
        paper_writer_timeout_seconds=int(app.get("paper_writer_timeout_seconds", 2400)),
        paper_writer_warning_remaining_seconds=int(
            app.get("paper_writer_warning_remaining_seconds", 600)
        ),
        codex=CodexConfig(
            binary=str(codex["binary"]),
            provider=str(codex["provider"]),
            base_url=str(codex["base_url"]),
            api_key_env=str(codex["api_key_env"]),
            history_persistence=str(codex["history_persistence"]),
            model_catalog=catalog_path,
            model_auto_compact_token_limit=compact_limit,
            use_docker_boundary=os.environ.get(
                "MATHLAB_CODEX_USE_DOCKER_BOUNDARY", "0"
            ).strip().lower() in {"1", "true", "yes", "on"},
        ),
        models=models,
        model_profiles=model_profiles,
        search=SearchConfig(
            theorem_primary_endpoint=str(search["theorem_primary_endpoint"]),
            theorem_fallback_endpoint=str(search["theorem_fallback_endpoint"]),
            arxiv_api_endpoint=str(search["arxiv_api_endpoint"]),
            max_results=int(search.get("max_results", 8)),
            timeout_seconds=int(search.get("timeout_seconds", 45)),
            max_literature_theorem_searches=int(search.get("max_literature_theorem_searches", 1)),
            max_literature_arxiv_searches=int(search.get("max_literature_arxiv_searches", 2)),
            max_literature_abstracts=int(search.get("max_literature_abstracts", 5)),
            max_literature_papers=int(search.get("max_literature_papers", 1)),
        ),
        publication_literature=PublicationLiteratureConfig(
            enabled=bool(publication_literature.get("enabled", True)),
            theorem_searches=int(publication_literature.get("theorem_searches", 6)),
            arxiv_searches=int(publication_literature.get("arxiv_searches", 6)),
            abstract_candidates=int(publication_literature.get("abstract_candidates", 30)),
            fulltext_papers=int(publication_literature.get("fulltext_papers", 4)),
            abstract_keep=int(publication_literature.get("abstract_keep", 5)),
        ),
    )
