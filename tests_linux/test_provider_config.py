from pathlib import Path

import pytest

from provider_config import apply_provider_config, extend_catalog


def test_provider_overrides_apply_to_planner_and_prover_profiles(tmp_path, monkeypatch):
    config = tmp_path / "provider.toml"
    config.write_text(
        """
[provider]
base_url = "https://provider.example/v1"
api_key_env = "EXAMPLE_KEY"
wire_api = "responses"
default_model = "example-medium"

[profiles.low]
model = "example-low"
reasoning_effort = "high"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MATHLAB_PROVIDER_CONFIG", str(config))
    raw = {
        "codex": {"provider": "old", "base_url": "old", "api_key_env": "OLD"},
        "models": {"contractor": {"model": "old", "reasoning_effort": "max"}},
        "model_profiles": {
            "low": {"contractor": {"model": "old", "reasoning_effort": "max"}}
        },
    }
    result = apply_provider_config(raw, "planner", root=Path("/tmp"))
    assert result["codex"]["base_url"] == "https://provider.example/v1"
    assert result["codex"]["api_key_env"] == "EXAMPLE_KEY"
    assert result["models"]["contractor"]["model"] == "example-medium"
    assert result["model_profiles"]["low"]["contractor"]["model"] == "example-low"


def test_chat_only_provider_is_rejected(tmp_path, monkeypatch):
    config = tmp_path / "provider.toml"
    config.write_text("[provider]\nwire_api = 'chat'\n", encoding="utf-8")
    monkeypatch.setenv("MATHLAB_PROVIDER_CONFIG", str(config))
    with pytest.raises(ValueError, match="Responses"):
        apply_provider_config({"codex": {}}, "planner", root=Path("/tmp"))


def test_unlisted_models_get_a_runtime_catalog_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHLAB_ALLOW_UNLISTED_MODELS", "1")
    monkeypatch.setenv("MATHLAB_PROVIDER_CONFIG", str(tmp_path / "missing.toml"))
    catalog = extend_catalog({"models": []}, {"custom-model"}, root=tmp_path)
    assert catalog["models"][0]["slug"] == "custom-model"
    assert catalog["models"][0]["base_instructions"]
    assert {item["effort"] for item in catalog["models"][0]["supported_reasoning_levels"]} >= {"high", "max"}
