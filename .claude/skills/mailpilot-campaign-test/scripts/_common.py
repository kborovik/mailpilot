"""Shared helpers for the campaign-test skill scripts.

Deterministic, no-LLM utilities: locate the repo and per-run artifact dir,
shell out to the ``mailpilot`` CLI and parse its JSON envelopes, and name the
reusable test scaffolding the agentic run depends on.

The skill tests the real outbound workflow agent
(``workflows/outbound-lab5-llm-lookup-work.toml``) against real contact data
without ever emailing a real contact. It does this by mirroring each selected
real contact's name/title/company onto a persistent alias-contact whose own
email is one of the nine inbound aliases, enrolling that alias-contact in an
ephemeral copy of the workflow, and letting the live agent draft and send to
the alias. Because the agent sends to the contact's stored email, and that
stored email is the alias, the real address is never a recipient.

Run every script via ``uv run python`` so the project venv (and the
``mailpilot`` console script + importable package) is on PATH.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

# Source account for every send.
SENDER_EMAIL = "outbound@lab5.ca"

# Target addresses. Each selected real contact is mirrored onto one of nine
# persistent alias-contacts whose own email is an ``inbound{1-9}@lab5.ca``
# alias; all nine aliases deliver into the ALIAS_MAILBOX account, which the
# skill syncs to confirm delivery. The contact's stored email IS the alias, so
# the agent -- which sends to the contact's email -- can only ever reach the
# alias, never the real address. The skill never starts ``mailpilot run``, so
# no auto-reply fires even though this mailbox carries an inbound workflow.
ALIAS_MAILBOX = "inbound@lab5.ca"
ALIASES = [f"inbound{number}@lab5.ca" for number in range(1, 10)]
MAX_ALIASES = len(ALIASES)

# Neutral parking company for the alias-contacts. Disabled so it stays out of
# ``company list`` and the lead-contacts discovery set (§V.96 / §V.114), and a
# ``.invalid`` domain so it can never collide with a real company (§V.90).
# Alias-contacts park here at rest; a run links them to the real company only
# for the duration of the run, then cleanup re-parks them here so the real
# company's contact_count is untouched between runs.
NEUTRAL_COMPANY_DOMAIN = "campaign-test.invalid"
NEUTRAL_COMPANY_NAME = "MailPilot Campaign Test"

# The outbound workflow whose agent this skill exercises. The default is the
# committed lab5.ca cold-outreach definition; ``--workflow-file`` overrides it.
DEFAULT_WORKFLOW_FILE = "workflows/outbound-lab5-llm-lookup-work.toml"


def repo_root() -> Path:
    """Return the repository root by walking up for ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def run_dir(run_id: str) -> Path:
    """Return (creating) the artifact dir: ``<repo>/.campaign-test/<run_id>``."""
    directory = repo_root() / ".campaign-test" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_json(path: Path) -> Any:
    """Parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as indented JSON (datetimes coerced to str)."""
    path.write_text(json.dumps(obj, indent=2, default=str))


def alias_for(sequence: int) -> str:
    """Map a 1-based contact sequence to its inbound alias (wraps past nine)."""
    return ALIASES[(sequence - 1) % MAX_ALIASES]


def ephemeral_workflow_name(run_id: str) -> str:
    """Name the per-run ephemeral workflow.

    Each run imports a fresh workflow under a unique name so the 30-day
    cold-send cooldown (§V.79, keyed on account+contact+workflow) never blocks
    a re-run: a fresh workflow id has no prior cold outbound. Named so an
    operator can spot and stop leftover test workflows in ``workflow list``.
    """
    return f"[campaign-test {run_id}]"


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
