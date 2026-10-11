#!/usr/bin/env python3
"""在隔离本地数据根中执行一次真实 GPT Low 一小时验收。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from mathlab_linux_cli import LocalRuntime, MathLabClient
from provider_config import load_provider_config


TERMINAL_RUN_STATES = {"completed", "failed", "interrupted"}
TERMINAL_GENERATION_STATES = {"completed", "failed", "interrupted"}


def _free_port() -> int:
    """申请一个短期 loopback 端口，避免验收和其他本地服务冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_generation(client: MathLabClient, conversation_id: str, poll_seconds: float) -> dict:
    """等待 Contract pipeline 完成，并返回最后一次 generation payload。"""
    last_status = None
    while True:
        payload = client.request("GET", f"/api/conversations/{conversation_id}/generation")
        attempt = payload.get("attempt") or {}
        status = str(attempt.get("status") or "unknown")
        if status != last_status:
            print(f"contract generation: {status}", flush=True)
            last_status = status
        if status in TERMINAL_GENERATION_STATES:
            if status != "completed":
                raise RuntimeError(f"Contract generation ended with {status}")
            return payload
        time.sleep(poll_seconds)


def _wait_run(
    client: MathLabClient,
    run_id: str,
    poll_seconds: float,
    max_wait_seconds: float,
) -> dict:
    """等待 Research Run 及出版链进入 terminal 状态。"""
    started = time.monotonic()
    last_status = None
    while True:
        payload = client.request("GET", f"/api/runs/{run_id}")
        status = str(payload.get("status") or "unknown")
        phase = str(payload.get("progress_phase") or payload.get("runtime_phase") or "")
        marker = f"{status}/{phase}"
        if marker != last_status:
            print(f"research run: {marker}", flush=True)
            last_status = marker
        if status in TERMINAL_RUN_STATES:
            return payload
        if time.monotonic() - started > max_wait_seconds:
            raise TimeoutError(f"Research Run exceeded {max_wait_seconds:.0f}s")
        time.sleep(poll_seconds)


def _check_provider() -> tuple[dict, str, str, str]:
    config = load_provider_config(ROOT)
    provider = dict(config.get("provider") or {})
    if str(provider.get("wire_api", "responses")).lower() != "responses":
        raise RuntimeError("live acceptance requires a Responses-compatible provider")
    base_url = str(provider.get("base_url", "")).rstrip("/")
    api_key_env = str(provider.get("api_key_env", "")).strip()
    low = dict((config.get("profiles") or {}).get("low") or {})
    model = str(low.get("model", "")).strip()
    effort = str(low.get("reasoning_effort", "")).strip()
    if not base_url or not api_key_env or not model:
        raise RuntimeError("provider.toml must define base_url, api_key_env and profiles.low.model")
    if "api.example.com" in base_url or model.startswith("your-model-"):
        raise RuntimeError("provider.toml still contains placeholder URL/model; no call was made")
    if not os.environ.get(api_key_env):
        raise RuntimeError(f"environment variable {api_key_env} is not set; no call was made")
    if os.environ.get("MATHLAB_OFFLINE_AGENTS", "0") == "1":
        raise RuntimeError("live acceptance refuses MATHLAB_OFFLINE_AGENTS=1")
    return provider, api_key_env, model, effort


def run_acceptance(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env", override=False)
    provider, api_key_env, model, effort = _check_provider()
    if not args.yes:
        raise RuntimeError("This performs a real one-hour paid run; pass --yes explicitly")

    if args.data_root:
        data_root = args.data_root.expanduser().resolve()
        data_root.mkdir(parents=True, exist_ok=True)
    else:
        data_root = Path(tempfile.mkdtemp(prefix="mathlab-linux-acceptance-"))
    os.environ["MATHLAB_DATA_ROOT"] = str(data_root)

    runtime = LocalRuntime(port=_free_port(), data_root=data_root, offline=False)
    client = MathLabClient(runtime.base_url)
    started_at = _now()
    try:
        runtime.start()
        draft = client.request(
            "POST",
            "/api/contract-drafts",
            json={
                "research_problem": args.problem,
                "contractor_profile": "low",
                "contract_language": "en",
            },
        )
        conversation_id = str(draft["conversation_id"])
        queued = client.request(
            "POST",
            f"/api/conversations/{conversation_id}/generate",
            json={"idempotency_key": f"live-acceptance-{time.time_ns()}"},
        )
        generation = _wait_generation(client, conversation_id, args.poll_seconds)
        detail = client.request("GET", f"/api/conversations/{conversation_id}")
        versions = detail.get("versions") or []
        if not versions:
            raise RuntimeError("Contract generation completed without an immutable version")
        version = int(versions[-1]["version"])
        policy = client.request(
            "PATCH",
            f"/api/conversations/{conversation_id}/execution-policy",
            json={"model_tier": "gpt_low", "duration_tier": "quick_1h"},
        )
        if policy.get("model_tier") != "gpt_low" or policy.get("duration_tier") != "quick_1h":
            raise RuntimeError(f"unexpected execution policy: {policy}")
        approval = client.request(
            "POST",
            f"/api/conversations/{conversation_id}/approve",
            json={"target_kind": "immutable_version", "version": version, "research_language": "en"},
        )
        run_id = str(approval["run_id"])
        run = _wait_run(client, run_id, args.poll_seconds, args.max_wait_seconds)
        report = {
            "acceptance": "MathLab Linux real provider acceptance",
            "provider": str(provider.get("provider", "configured")),
            "profile": "gpt_low",
            "configured_model": model,
            "configured_reasoning_effort": effort,
            "api_key_env": api_key_env,
            "conversation_id": conversation_id,
            "contract_version": version,
            "generation_attempt_id": generation.get("attempt", {}).get("generation_attempt_id"),
            "queued_generation": queued,
            "run_id": run_id,
            "run_status": run.get("status"),
            "execution_policy": run.get("execution_policy"),
            "research_budget_seconds": run.get("research_budget_seconds"),
            "research_elapsed_seconds": run.get("research_elapsed_seconds"),
            "usage": run.get("usage"),
            "final_paper_pdf_path": run.get("final_paper_pdf_path"),
            "final_paper_tex_path": run.get("final_paper_tex_path"),
            "started_at": started_at,
            "finished_at": _now(),
            "data_root": str(data_root),
        }
        report_path = data_root / "acceptance-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        pdf_ref = str(run.get("final_paper_pdf_path") or "")
        pdf_exists = bool(pdf_ref) and (data_root / pdf_ref).is_file()
        if run.get("status") != "completed":
            raise RuntimeError(f"Run ended with {run.get('status')}; report: {report_path}")
        if run.get("execution_policy", {}).get("duration_seconds") != 3600:
            raise RuntimeError(f"Run did not freeze quick_1h; report: {report_path}")
        if not pdf_exists:
            raise RuntimeError(f"Run completed without a final PDF; report: {report_path}")
        print(f"acceptance passed: run={run_id}")
        print(f"report: {report_path}")
        print(f"pdf: {data_root / pdf_ref}")
        return 0
    finally:
        client.close()
        runtime.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real isolated MathLab Linux GPT Low acceptance")
    parser.add_argument("--problem", required=True, help="research problem used for the temporary Contract")
    parser.add_argument("--data-root", type=Path, help="persistent acceptance output directory")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--max-wait-seconds", type=float, default=14400.0)
    parser.add_argument("--yes", action="store_true", help="acknowledge real provider cost")
    args = parser.parse_args()
    try:
        raise SystemExit(run_acceptance(args))
    except Exception as exc:
        print(f"acceptance failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
