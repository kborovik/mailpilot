"""Shared helpers for the campaign-test skill scripts.

Deterministic, no-LLM utilities: locate the repo and per-run artifact dir,
shell out to the ``mailpilot`` CLI and parse its JSON envelopes, load the
scenario catalog, and name the scaffolding the multi-step test depends on.

The skill tests the **full multi-step flow** of the real outbound workflow agent
(``workflows/ai-engineering.toml``): the agent sends a cold Touch 1, the test
replies to that email with content crafted to drive each branch of the
workflow's "Handling replies" section, the agent handles the reply, and the test
verifies the branch the agent took. No real prospect is ever emailed -- the only
recipient is the controlled ``inbound@lab5.ca`` mailbox.

The prospect is a single contact whose own email IS ``inbound@lab5.ca``. Inbound
contact attribution is by the From address, and a reply can only be sent from a
real mailbox, so one prospect mailbox means one prospect contact. Scenarios stay
independent because each gets its **own ephemeral workflow** -- an enrollment is
per (workflow, contact), so N ephemeral workflows give N independent enrollments
for the same contact, and a fresh workflow id also dodges the 30-day cold-send
cooldown (§V.79).

Run every script via ``uv run python`` so the project venv (and the
``mailpilot`` console script + importable package) is on PATH.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

# Source account for every Touch 1 send.
SENDER_EMAIL = "outbound@lab5.ca"

# Required outbound account signature for campaign-test sends (§V.151).
# Nested account.signature fields; preflight blocks on missing/mismatch.
REQUIRED_OUTBOUND_SIGNATURE: dict[str, str] = {
    "full_name": "Konstantin Borovi",
    "title": "DevOps Engineer",
    "website": "https://lab5.ca",
    "phone": "+1-416-670-0621",
}

# The prospect mailbox AND the prospect contact's email. The agent sends Touch 1
# to this address (so no real prospect is reached), and the test replies from
# this same mailbox, so the reply's From maps the inbound reply back to this one
# prospect contact (sync attributes an inbound email to the contact matching the
# From address). The skill never starts ``mailpilot run``; reply handling is
# driven scoped instead (see ``handle_replies.py``), so no auto-reply loop fires.
PROSPECT_MAILBOX = "inbound@lab5.ca"
PROSPECT_EMAIL = "inbound@lab5.ca"

# Neutral parking company for the prospect contact at rest. Disabled so it stays
# out of ``company list`` and the lead-contacts discovery set (§V.96 / §V.114),
# and a ``.invalid`` domain so it can never collide with a real company (§V.90).
# The prospect contact parks here at rest; a run links it to the real grounding
# company only for the duration of the run, then cleanup re-parks it here so the
# real company's contact_count is untouched between runs.
NEUTRAL_COMPANY_DOMAIN = "campaign-test.invalid"
NEUTRAL_COMPANY_NAME = "MailPilot Campaign Test"

# The outbound workflow whose agent this skill exercises. The default is the
# committed lab5.ca cold-outreach definition; ``--workflow-file`` overrides it.
DEFAULT_WORKFLOW_FILE = "workflows/ai-engineering.toml"

# The scenario catalog drives the reply branches under test. One ephemeral
# workflow + one enrollment + one Touch 1 + one crafted reply per scenario.
SCENARIOS_FILE = "references/reply-scenarios.json"


def repo_root() -> Path:
    """Return the repository root by walking up for ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def skill_root() -> Path:
    """Return the skill directory (parent of ``scripts/``)."""
    return Path(__file__).resolve().parent.parent


def run_dir(run_id: str) -> Path:
    """Return (creating) the artifact dir: ``<repo>/reports/campaign-test/<run_id>``."""
    directory = repo_root() / "reports" / "campaign-test" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_json(path: Path) -> Any:
    """Parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as indented JSON (datetimes coerced to str)."""
    path.write_text(json.dumps(obj, indent=2, default=str))


def load_scenarios() -> list[dict[str, Any]]:
    """Return the reply-branch scenario catalog (list of scenario dicts)."""
    path = skill_root() / SCENARIOS_FILE
    return read_json(path)["scenarios"]


def ephemeral_workflow_name(run_id: str, scenario_key: str) -> str:
    """Name a per-scenario ephemeral workflow.

    Each scenario imports a fresh workflow under a unique name so its enrollment
    is independent of the other scenarios' and the 30-day cold-send cooldown
    (§V.79, keyed on account+contact+workflow) never blocks a re-run: a fresh
    workflow id has no prior cold outbound. Named so an operator can spot and
    stop leftover test workflows in ``workflow list``. The name is kebab-case
    because ``workflow import`` enforces kebab names equal to the file stem
    (§V.103), so underscores in the run id become hyphens. The scenario rides
    as an opaque hash token, never the key: the agent prompt includes the
    workflow name (``invoke.py`` "Workflow: {name}"), and a readable key like
    ``wrong-person`` primes the agent toward the branch under test -- it made
    Touch 1 turns inspect the contact record and decline the send. The
    scenario-to-name mapping lives in ``scaffold.json``.
    """
    token = hashlib.sha1(scenario_key.encode()).hexdigest()[:8]
    return f"campaign-test-{run_id}-{token}".replace("_", "-")


def mp(args: list[str], *, check: bool = True) -> dict[str, Any]:
    """Run a ``mailpilot`` subcommand and return its parsed JSON stdout.

    Args:
        args: CLI arguments after ``mailpilot`` (e.g. ``["contact", "list"]``).
        check: Raise ``RuntimeError`` on non-zero exit when True.

    Returns:
        Parsed JSON dict (the CLI's single-line JSON envelope, §V.4). On error
        with ``check=False`` returns the error envelope when parseable (the
        error envelope rides stderr per §V.3, so stderr is parsed on failure).
    """
    proc = subprocess.run(
        ["mailpilot", *args], capture_output=True, text=True, check=False
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    payload: dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"_raw_stdout": stdout}
    if proc.returncode != 0:
        if not payload and stderr:
            try:
                payload = json.loads(stderr)
            except json.JSONDecodeError:
                payload = {"error": "cli_error", "message": stderr}
        if check:
            raise RuntimeError(
                f"mailpilot {' '.join(args)} failed: {payload or stderr}"
            )
    return payload


def resolve_account(email: str) -> dict[str, Any] | None:
    """Return the account row for an email address, or None if absent."""
    data = mp(["account", "list", "--include-disabled", "--limit", "100"], check=False)
    for account in data.get("accounts", []):
        if str(account.get("email", "")).lower() == email.lower():
            return account
    return None


def resolve_account_id(email: str) -> str | None:
    """Return the UUIDv7 account id for an email address, or None if absent."""
    account = resolve_account(email)
    return account.get("id") if account else None


def clear_contact_notes(contact_ref: str) -> int:
    """Delete every note on a contact via owner bulk remove; return count.

    Uses `note remove --contact-email <ref> --yes` (§V.14). Idempotent and
    best-effort: a missing contact or zero notes yields 0.
    """
    result = mp(
        ["note", "remove", "--contact-email", contact_ref, "--yes"],
        check=False,
    )
    if not result.get("ok"):
        return 0
    removed = result.get("notes_removed", {})
    if isinstance(removed, dict):
        count = removed.get("removed_count")
        if isinstance(count, int):
            return count
    return 0
