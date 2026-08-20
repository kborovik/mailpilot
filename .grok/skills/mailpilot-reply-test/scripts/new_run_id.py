"""Print a fresh date-stamped run id for a test run.

A standalone helper (no shell-special characters on the command line) so the
orchestrator can mint a run id portably across shells:

    uv run python scripts/new_run_id.py

The id is ``YYYY-MM-DD-HHMMSS_<hex>``: the date-time prefix sorts report folders
chronologically and reads at a glance, the hex suffix keeps each run unique.
Capture the printed value and pass it verbatim as ``--run-id`` to every other
script -- shell variables do not persist across separate tool calls.
"""

from __future__ import annotations

import secrets
from datetime import datetime

stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
print(f"{stamp}_{secrets.token_hex(4)}")
