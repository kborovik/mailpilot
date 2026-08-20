"""Shared helpers for the mailpilot-reply-test skill scripts.

Deterministic, no-LLM utilities: locate the repo and per-run artifact dir,
shell out to the ``mailpilot`` CLI and parse its JSON envelopes, and build /
parse the ``MP-TEST`` subject tag that correlates a sent question with the
agent's threaded reply.

Run every script via ``uv run python`` so the project venv (and the
``mailpilot`` console script + importable package) is on PATH.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_DRIVE_FOLDER_ID = "1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat"
OUTBOUND_EMAIL = "outbound@lab5.ca"
INBOUND_EMAIL = "inbound@lab5.ca"

# Reply subject is "Re: [MP-TEST <run_id> <case_id>] <hint>"; we match the tag.
# The run id is date-stamped (``YYYY-MM-DD-HHMMSS_<hex>``), so the token spans
# digits, dashes, an underscore, and hex -- it is space-delimited from case_id.
SUBJECT_TAG_RE = re.compile(
    r"\[MP-TEST (?P<run_id>[0-9a-f_-]+) (?P<case_id>qa-[a-z]+-\d+)\]"
)


def repo_root() -> Path:
    """Return the repository root by walking up for ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def run_dir(run_id: str) -> Path:
    """Return (creating) the artifact dir for a run: ``<repo>/reports/reply-test/<run_id>``."""
    directory = repo_root() / "reports" / "reply-test" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def qa_pairs_path() -> Path:
    """Path to the QA fixture bundled with the skill (``assets/QA-Pairs.json``).

    Anchored on the skill directory (``scripts/``'s parent), not the repo root,
    so the skill stays self-contained and travels as one unit.
    """
    skill_dir = Path(__file__).resolve().parent.parent
    return skill_dir / "assets" / "QA-Pairs.json"


def read_json(path: Path) -> Any:
    """Parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as indented JSON (datetimes coerced to str)."""
    path.write_text(json.dumps(obj, indent=2, default=str))


def make_subject(run_id: str, case_id: str, hint: str) -> str:
    """Build a tagged subject the reply will preserve (and we can match)."""
    clean_hint = " ".join(hint.split())[:60]
    return f"[MP-TEST {run_id} {case_id}] {clean_hint}"


def parse_subject(subject: str) -> tuple[str, str] | None:
    """Extract ``(run_id, case_id)`` from a (possibly ``Re:``-prefixed) subject."""
    match = SUBJECT_TAG_RE.search(subject or "")
    if match is None:
        return None
    return match.group("run_id"), match.group("case_id")


def mp(args: list[str], *, check: bool = True) -> dict[str, Any]:
    """Run a ``mailpilot`` subcommand and return its parsed JSON stdout.

    Args:
        args: CLI arguments after ``mailpilot`` (e.g. ``["email", "list"]``).
        check: Raise ``RuntimeError`` on non-zero exit when True.

    Returns:
        Parsed JSON dict (the CLI's single-line JSON envelope, §V.4). On error
        with ``check=False`` returns the error envelope when parseable.
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


def resolve_account_id(email: str) -> str | None:
    """Return the UUIDv7 account id for an email address, or None if absent."""
    data = mp(["account", "list", "--limit", "100"], check=False)
    for account in data.get("accounts", []):
        if str(account.get("email", "")).lower() == email.lower():
            return account.get("id")
    return None
