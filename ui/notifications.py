"""macOS notifications via osascript."""

import subprocess
import sys


def notify_macos(title: str, message: str) -> None:
    """Send a macOS notification (fire-and-forget)."""
    if sys.platform != "darwin":
        return
    try:
        safe_msg = message.replace('"', '\\"').replace("'", "\\'")
        safe_title = title.replace('"', '\\"')
        script = f'display notification "{safe_msg}" with title "{safe_title}"'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
