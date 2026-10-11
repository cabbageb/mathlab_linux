from __future__ import annotations

from pathlib import Path
import argparse
import asyncio
import os
import signal
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
for relative in ("core/src", "planner/src", "prover/src"):
    value = str(ROOT / relative)
    if value not in sys.path:
        sys.path.insert(0, value)

from math_lab_core.vnext.runner import ResearchRunnerService
from math_lab_core.vnext.settings import load_settings


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="MathLab Dynamic Research Runner")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    os.environ["MATHLAB_PROCESS_ROLE"] = "runner"
    if args.offline:
        os.environ["MATHLAB_OFFLINE_AGENTS"] = "1"
    settings = load_settings(args.config)
    service = ResearchRunnerService(settings)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for signame in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signame, service.request_stop)
            except NotImplementedError:
                pass
        await service.serve()

    asyncio.run(run())


if __name__ == "__main__":
    main()
