"""Start the ``mailpilot run`` reply loop, detached, for the test window.

The loop is the only path that drains the task queue into auto-replies
(``account sync`` alone does not). It is daemonized with ``start_new_session``
so it survives the launching sub-agent's exit, logs to ``run.log``, and runs
with ``MAILPILOT_RUN_INTERVAL`` low so the polling fallback picks up test mail
quickly -- no persistent ``config set`` mutation.

NOTE: while up, the loop processes ALL accounts. Always pair with
run_loop_stop.py at teardown (manual fallback: ``pkill -f "mailpilot run"``).

Usage:
    uv run python scripts/run_loop_start.py --run-id <id> [--interval 10] [--wait 45]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from _common import repo_root, run_dir

READY_MARKERS = ("event=loop.start", "loop.start", "Pub/Sub", "polling-only")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--wait", type=int, default=45)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    pid_file = directory / "run.pid"
    log_file = directory / "run.log"

    if pid_file.exists():
        existing = int(pid_file.read_text().strip() or "0")
        if existing and _alive(existing):
            print(
                json.dumps(
                    {"started": False, "reason": "already running", "pid": existing}
                )
            )
            return 0

    env = {**os.environ, "MAILPILOT_RUN_INTERVAL": str(args.interval)}
    # Handle is intentionally left open for the detached child to own.
    log_handle = open(log_file, "ab")  # noqa: SIM115
    process = subprocess.Popen(
        ["mailpilot", "run"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        cwd=str(repo_root()),
    )
    pid_file.write_text(str(process.pid))

    ready = False
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (
                log_file.read_text(errors="replace")[-1500:]
                if log_file.exists()
                else ""
            )
            print(
                json.dumps(
                    {
                        "started": False,
                        "reason": "process exited",
                        "pid": process.pid,
                        "log_tail": tail,
                    }
                )
            )
            return 1
        text = log_file.read_text(errors="replace") if log_file.exists() else ""
        if any(marker in text for marker in READY_MARKERS):
            ready = True
            break
        time.sleep(1.0)

    print(
        json.dumps(
            {
                "started": True,
                "pid": process.pid,
                "ready": ready,
                "log": str(log_file),
                "interval": args.interval,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
