from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import os
import sys
import threading

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
for relative in ("core/src", "planner/src", "prover/src"):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from math_lab_core.vnext.app import create_vnext_app
from math_lab_core.vnext.settings import VNextSettings, load_settings
from math_planner.browser_launcher import open_default_browser


def build_app(config_path: Path | None = None):
    """装配 Contract-first Planner 与 Dynamic Research Graph Prover。"""
    settings = load_settings(config_path or ROOT / "config.toml")
    return settings, create_vnext_app(settings)


def _schedule_browser_open(url: str) -> None:
    threading.Timer(0.8, open_default_browser, args=(url,)).start()


def _browser_host(host: str) -> str:
    return "localhost" if host in {"0.0.0.0", "::"} else host


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="MathLab — Contract-first AI Mathematical Research"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--offline",
        "--offline-agents",
        dest="offline_agents",
        action="store_true",
        help="使用显式离线演示角色，不执行付费模型调用",
    )
    parser.add_argument(
        "--database-url",
        help="仅覆盖本次进程的数据库 URL；生产环境建议使用 MATHLAB_DATABASE_URL",
    )
    args = parser.parse_args()
    if args.offline_agents:
        os.environ["MATHLAB_OFFLINE_AGENTS"] = "1"
    if args.database_url:
        os.environ["MATHLAB_DATABASE_URL"] = args.database_url
    settings = load_settings(args.config)
    if args.host or args.port:
        settings = replace(
            settings,
            host=args.host or settings.host,
            port=args.port or settings.port,
        )
    app = create_vnext_app(settings)
    if settings.open_browser and not args.no_browser:
        _schedule_browser_open(
            f"http://{_browser_host(settings.host)}:{settings.port}"
        )
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
