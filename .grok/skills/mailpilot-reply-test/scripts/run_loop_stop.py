"""Stop the detached ``mailpilot run`` loop started for a test run.

Reads ``run.pid``, SIGTERMs the loop's process group (SIGKILL fallback), and
removes the pid file. Idempotent: a no-op when no pid file exists or the process
is already gone. MUST run at teardown of every test, even after a failed phase.

Usage:
    uv run python scripts/run_loop_stop.py --run-id <id>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import time

from _common import run_dir


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError, PermissionError:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    pid_file = run_dir(args.run_id) / "run.pid"
    if not pid_file.exists():
        print(json.dumps({"stopped": False, "reason": "no pid file"}))
        return 0

    pid = int(pid_file.read_text().strip() or "0")
    if not pid or not _alive(pid):
        pid_file.unlink(missing_ok=True)
        print(json.dumps({"stopped": False, "reason": "not running", "pid": pid}))
        return 0

    _signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.5)
    if _alive(pid):
        _signal_group(pid, signal.SIGKILL)
        time.sleep(1.0)

    pid_file.unlink(missing_ok=True)
    print(json.dumps({"stopped": True, "pid": pid, "killed": _alive(pid) is False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
