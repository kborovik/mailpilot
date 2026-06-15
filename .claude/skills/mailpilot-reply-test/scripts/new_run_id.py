"""Print a fresh 8-character hex run id for a test run.

A standalone helper (no shell-special characters on the command line) so the
orchestrator can mint a run id portably across shells:

    uv run python scripts/new_run_id.py

Capture the printed value and pass it verbatim as ``--run-id`` to every other
script -- shell variables do not persist across separate tool calls.
"""

from __future__ import annotations

import secrets

print(secrets.token_hex(4))
