from dataclasses import replace
from pathlib import Path

from mathlab_linux_cli import _set_policy
from math_lab_core.vnext.app import create_vnext_app
from math_lab_core.vnext.settings import load_settings


class FakeClient:
    conversation_id = "chat_test"

    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"model_tier": "gpt_low", "duration_tier": "quick_1h", "duration_seconds": 3600}


def test_cli_policy_sets_low_one_hour_policy():
    client = FakeClient()
    _set_policy(client, "gpt_low quick_1h")
    method, path, kwargs = client.calls[0]
    assert method == "PATCH"
    assert path == "/api/conversations/chat_test/execution-policy"
    assert kwargs["json"] == {"model_tier": "gpt_low", "duration_tier": "quick_1h"}


def test_app_can_be_created_without_browser_assets(tmp_path, monkeypatch):
    config = Path(__file__).parents[1] / "config.toml"
    monkeypatch.setenv("MATHLAB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MATHLAB_ALLOW_UNLISTED_MODELS", "1")
    settings = load_settings(config)
    settings = replace(settings, root=tmp_path)
    app = create_vnext_app(settings)
    assert not (tmp_path / "browser").exists()
    assert not any(getattr(route, "path", None) == "/static" for route in app.routes)
