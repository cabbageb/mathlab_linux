"""MathLab Linux 的单用户 CLI：启动本地 runtime，并通过 loopback API 管理会话。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from math_lab_core.vnext.contract_schema import parse_contract, render_contract, validate_contract
from provider_config import load_provider_config


ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 3264
DEFAULT_USER = "dev_admin"


class LocalRuntime:
    """启动单机 combined runtime，并在退出时只回收本次启动的进程。"""

    def __init__(self, *, port: int, data_root: Path, offline: bool = False) -> None:
        self.port = port
        self.data_root = data_root.expanduser().resolve()
        self.base_url = f"http://127.0.0.1:{port}"
        self.offline = offline
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any = None

    def start(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "MATHLAB_ENV": "local",
                "MATHLAB_PROCESS_ROLE": "combined",
                "MATHLAB_TEST_AUTH_BYPASS": "1",
                "MATHLAB_SEED_DEV_USERS": "1",
                "MATHLAB_BOOTSTRAP_ADMIN_EMAIL": "",
                "MATHLAB_DATA_ROOT": str(self.data_root),
                "MATHLAB_DATABASE_URL": f"sqlite+pysqlite:///{self.data_root / 'mathlab.sqlite3'}",
                "MATHLAB_REQUIRE_LIVE_CREDIT_PRICING": "0",
                "MATHLAB_ALLOW_UNLISTED_MODELS": "1",
                "MATHLAB_PROVIDER_CONFIG": os.environ.get(
                    "MATHLAB_PROVIDER_CONFIG", str(ROOT / "config" / "provider.toml")
                ),
            }
        )
        if self.offline:
            env["MATHLAB_OFFLINE_AGENTS"] = "1"
        log_path = self.data_root / "runtime" / "local-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "math_lab.py"),
                "--config",
                str(ROOT / "config.toml"),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-browser",
            ],
            cwd=ROOT,
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        with httpx.Client(timeout=2.0) as client:
            for _ in range(60):
                if self.process.poll() is not None:
                    raise RuntimeError(f"local runtime exited; inspect {log_path}")
                try:
                    response = client.get(f"{self.base_url}/api/health")
                    if response.is_success:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
        raise RuntimeError(f"local runtime did not become healthy; inspect {log_path}")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_handle is not None:
            self.log_handle.close()


class MathLabClient:
    """调用 loopback API，保持 CLI 与正式 Web API 共用同一业务路径。"""

    def __init__(self, base_url: str, user_id: str = DEFAULT_USER) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.conversation_id: str | None = None
        self.run_id: str | None = None
        self.client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-MathLab-User"] = self.user_id
        response = self.client.request(method, self.base_url + path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        if not response.content:
            return None
        return response.json()


def _profile_model(profile: str) -> tuple[str, str]:
    config = load_provider_config(ROOT)
    profiles = config.get("profiles") or {}
    selected = profiles.get(profile) or profiles.get("medium") or {}
    return str(selected.get("model", "")), str(selected.get("reasoning_effort", "high"))


def test_api(profile: str, *, yes: bool = False) -> int:
    """执行一次最小 Responses 调用并输出不含凭据的能力报告。"""
    config = load_provider_config(ROOT)
    provider = config.get("provider") or {}
    base_url = str(provider.get("base_url", "")).rstrip("/")
    api_key_env = str(provider.get("api_key_env", ""))
    model, effort = _profile_model(profile)
    api_key = os.environ.get(api_key_env, "")
    if not base_url or not api_key_env or not model:
        print("provider.toml 缺少 base_url、api_key_env 或 profile model", file=sys.stderr)
        return 2
    if not api_key:
        print(f"环境变量 {api_key_env} 未设置；不会发起调用", file=sys.stderr)
        return 2
    if not yes:
        answer = input(f"This performs one small paid call using {model}. Continue? [y/N] ")
        if answer.strip().lower() != "y":
            print("cancelled")
            return 0
    payload: dict[str, Any] = {
        "model": model,
        "input": "Reply with exactly OK.",
        "max_output_tokens": 16,
        "store": False,
    }
    if effort:
        payload["reasoning"] = {"effort": effort}
    try:
        response = httpx.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=45.0,
        )
    except httpx.HTTPError as exc:
        print(f"transport error: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"status: {response.status_code}")
    print(f"endpoint: {base_url}/responses")
    print(f"model: {model}")
    try:
        body = response.json()
    except ValueError:
        print("response: non-json")
        return 1
    usage = body.get("usage") or {}
    print(f"usage fields: {', '.join(sorted(str(key) for key in usage)) or '(none)'}")
    if response.is_success:
        text = str(body.get("output_text") or "")
        print(f"output preview: {text[:120]!r}")
        return 0
    error = body.get("error") or body.get("detail") or body
    print(f"error summary: {str(error)[:300]}", file=sys.stderr)
    return 1


def _print_sessions(client: MathLabClient) -> None:
    payload = client.request("GET", "/api/bootstrap")
    for item in payload.get("conversations", []):
        marker = "*" if item.get("conversation_id") == getattr(client, "conversation_id", None) else " "
        print(f"{marker} {item.get('conversation_id')}  {item.get('display_name') or item.get('title') or 'untitled'}  {item.get('status')}")


def _current_detail(client: MathLabClient) -> dict[str, Any]:
    conversation_id = getattr(client, "conversation_id", None)
    if not conversation_id:
        raise RuntimeError("先使用 /new 或 /use 选择会话")
    return client.request("GET", f"/api/conversations/{conversation_id}")


def _show(client: MathLabClient) -> None:
    detail = _current_detail(client)
    print(f"conversation: {detail.get('conversation_id')}  status: {detail.get('status')}")
    if detail.get("working_draft"):
        draft = detail["working_draft"]
        print(render_contract(draft["title"], draft["sections"]))
    elif detail.get("versions"):
        print(detail["versions"][-1]["markdown"])
    elif detail.get("request_draft"):
        print(json.dumps(detail["request_draft"], ensure_ascii=False, indent=2))
    else:
        print("(no contract yet)")


def _set_policy(client: MathLabClient, argument: str) -> None:
    """在批准前冻结本地会话的模型档位和研究时长。"""
    if not client.conversation_id:
        raise RuntimeError("先选择会话")
    parts = shlex.split(argument)
    if len(parts) != 2:
        raise RuntimeError("用法：/policy <model_tier> <duration_tier>")
    result = client.request(
        "PATCH",
        f"/api/conversations/{client.conversation_id}/execution-policy",
        json={"model_tier": parts[0], "duration_tier": parts[1]},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _generate(client: MathLabClient) -> None:
    conversation_id = getattr(client, "conversation_id", None)
    if not conversation_id:
        raise RuntimeError("先选择会话")
    queued = client.request(
        "POST", f"/api/conversations/{conversation_id}/generate", json={"idempotency_key": f"cli-{time.time_ns()}"}
    )
    attempt_id = queued.get("generation_attempt_id")
    print(f"generation queued: {attempt_id}")
    while True:
        detail = client.request("GET", f"/api/conversations/{conversation_id}/generation")
        attempt = detail.get("attempt") or {}
        status = attempt.get("status")
        print(f"generation: {status}")
        if status not in {"queued", "running", "repairing", "interrupted"}:
            break
        time.sleep(2)


def _edit(client: MathLabClient) -> None:
    detail = _current_detail(client)
    if detail.get("versions"):
        source = detail["versions"][-1]["markdown"]
    elif detail.get("working_draft"):
        draft = detail["working_draft"]
        source = render_contract(draft["title"], draft["sections"])
    else:
        raise RuntimeError("当前会话还没有可编辑的 Contract")
    edit_dir = Path(os.environ.get("MATHLAB_DATA_ROOT", str(Path.home() / ".local/share/mathlab_linux"))).expanduser() / "cli-edits"
    edit_dir.mkdir(parents=True, exist_ok=True)
    path = edit_dir / f"{client.conversation_id}.md"
    path.write_text(source, encoding="utf-8")
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    subprocess.run(shlex.split(editor) + [str(path)], check=True)
    edited = parse_contract(path.read_text(encoding="utf-8"))
    validation = validate_contract(edited)
    if validation.errors:
        raise RuntimeError("Contract 校验失败：\n" + "\n".join(validation.errors))
    draft = client.request("POST", f"/api/conversations/{client.conversation_id}/draft", json={})
    revision = int(draft.get("draft_revision", 0))
    base_version = int(draft.get("base_version", 1))
    editable_keys = set(detail.get("editable_section_keys") or edited.editable_sections().keys())
    for key, content in edited.sections.items():
        if key not in editable_keys:
            continue
        result = client.request(
            "PATCH",
            f"/api/conversations/{client.conversation_id}/draft",
            json={
                "section_key": key,
                "content": content,
                "expected_revision": revision,
                "base_version": base_version,
            },
        )
        revision = int(result.get("draft_revision", revision + 1))
    print(f"working draft updated from {path}")


def repl(client: MathLabClient) -> None:
    print("MathLab Linux CLI. Type /help for commands.")
    while True:
        try:
            raw = input("mathlab> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        if not raw.startswith("/"):
            print("Use /help for commands.")
            continue
        command, _, argument = raw.partition(" ")
        try:
            if command == "/help":
                print("/new <problem>  /sessions  /use <id>  /show  /generate  /edit  /commit  /policy <model_tier> <duration_tier>  /approve  /status  /events  /test_api  /quit")
            elif command == "/sessions":
                _print_sessions(client)
            elif command == "/new":
                if not argument.strip():
                    raise RuntimeError("用法：/new <research problem>")
                result = client.request(
                    "POST",
                    "/api/contract-drafts",
                    json={
                        "research_problem": argument.strip(),
                        "contractor_profile": "medium",
                        "contract_language": "en",
                    },
                )
                client.conversation_id = result["conversation_id"]
                print(f"created and selected {client.conversation_id}")
            elif command == "/use":
                if not argument.strip():
                    raise RuntimeError("用法：/use <conversation_id>")
                client.conversation_id = argument.strip()
                _current_detail(client)
                print(f"selected {client.conversation_id}")
            elif command == "/show":
                _show(client)
            elif command == "/generate":
                _generate(client)
            elif command == "/edit":
                _edit(client)
            elif command == "/policy":
                _set_policy(client, argument)
            elif command == "/commit":
                result = client.request("POST", f"/api/conversations/{client.conversation_id}/draft/commit")
                print(f"working draft committed as Contract v{result.get('version')}")
            elif command == "/approve":
                detail = _current_detail(client)
                version = detail.get("versions", [])[-1].get("version") if detail.get("versions") else None
                result = client.request(
                    "POST",
                    f"/api/conversations/{client.conversation_id}/approve",
                    json={"target_kind": "immutable_version", "version": version, "research_language": "en"},
                )
                client.run_id = result.get("run_id")
                print(f"approved; run_id={client.run_id} status={result.get('status')}")
            elif command == "/status":
                detail = _current_detail(client)
                run = detail.get("run") or detail.get("research_run")
                if run is None and client.run_id:
                    run = client.request("GET", f"/api/runs/{client.run_id}")
                print(json.dumps({"conversation_id": detail.get("conversation_id"), "status": detail.get("status"), "run": run}, ensure_ascii=False, indent=2))
            elif command == "/events":
                detail = _current_detail(client)
                run = detail.get("run") or detail.get("research_run")
                if run is None and client.run_id:
                    run = {"run_id": client.run_id}
                if not run:
                    print("no run")
                else:
                    print(json.dumps(client.request("GET", f"/api/runs/{run['run_id']}/public-events"), ensure_ascii=False, indent=2))
            elif command == "/test_api":
                test_api(argument.strip() or "medium")
            elif command in {"/quit", "/exit"}:
                return
            else:
                print("Unknown command. Use /help.")
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="MathLab Linux single-user CLI")
    parser.add_argument("--offline", action="store_true", help="启动无付费离线 backend")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MATHLAB_PORT", DEFAULT_PORT)))
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("command", nargs="?", choices=["test-api"])
    parser.add_argument("--profile", default="medium")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command == "test-api":
        raise SystemExit(test_api(args.profile, yes=args.yes))
    data_root = args.data_root or Path(os.environ.get("MATHLAB_DATA_ROOT", "~/.local/share/mathlab_linux"))
    # Keep CLI-side artifacts (for example /edit Markdown files) beside the
    # runtime database when --data-root is supplied, just like the child
    # combined runtime.
    os.environ["MATHLAB_DATA_ROOT"] = str(data_root.expanduser())
    runtime = LocalRuntime(port=args.port, data_root=data_root, offline=args.offline)
    client = MathLabClient(runtime.base_url)
    try:
        runtime.start()
        repl(client)
    finally:
        client.close()
        runtime.stop()


if __name__ == "__main__":
    main()
