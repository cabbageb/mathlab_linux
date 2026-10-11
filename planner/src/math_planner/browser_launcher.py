from __future__ import annotations

import os
import subprocess
import webbrowser


def open_default_browser(url: str) -> None:
    """在 WSL 中通过 Windows 默认浏览器打开网址，其他环境使用 webbrowser。"""
    if "microsoft" in os.uname().release.lower() or os.environ.get("WSL_DISTRO_NAME"):
        subprocess.Popen(
            [
                "/bin/bash",
                "-lc",
                'cd /mnt/c && /mnt/c/Windows/System32/cmd.exe /d /c start "" "$1"',
                "math-planner-browser",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    webbrowser.open(url)
