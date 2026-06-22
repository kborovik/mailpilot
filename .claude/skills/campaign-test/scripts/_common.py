"""Shared helpers for the campaign-test skill scripts.

Deterministic, no-LLM utilities: locate the repo and per-run artifact dir,
shell out to the ``mailpilot`` CLI and parse its JSON envelopes, parse a
campaign Markdown file into a subject template plus a body template, and
substitute per-contact personalization placeholders.

Run every script via ``uv run python`` so the project venv (and the
``mailpilot`` console script + importable package) is on PATH.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

# Sending account and the safe sink. Discovered contacts are the personalization
# DATA source only -- the real outbound recipient is ALWAYS the sink, never a
# discovered external address. The sink carries no active workflow, so even if
# ``mailpilot run`` were up no auto-reply would fire.
SENDER_EMAIL = "outbound@lab5.ca"
SINK_EMAIL = "hello@lab5.ca"

# Subject tag that correlates a sent test message with its delivered copy.
SUBJECT_TAG_PREFIX = "CAMPAIGN-TEST"
SUBJECT_TAG_RE = re.compile(r"\[CAMPAIGN-TEST (?P<run_id>[0-9a-f]+) (?P<seq>\d+)\]")

# Placeholder tokens the campaign body/subject may use. Each maps to a
# ``ContactSummary`` field (``company`` -> ``company_domain``); a NULL field
# substitutes the listed fallback and is recorded as a personalization gap.
PLACEHOLDER_FALLBACKS = {
    "first_name": "there",
    "last_name": "",
    "full_name": "there",
    "title": "",
    "company": "your company",
    "email": "",
}
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


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


def parse_campaign(path: Path) -> dict[str, str]:
    """Parse a campaign Markdown file into subject and body templates.

    Format: the first non-blank line must be ``Subject: <text>``; everything
    after the following blank line is the Markdown body. Both may carry
    ``{placeholder}`` tokens. Raises ``ValueError`` when the subject line is
    missing or the body is empty.
    """
    text = path.read_text()
    lines = text.splitlines()
    subject: str | None = None
    body_start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = re.match(r"^subject:\s*(.*)$", line, re.IGNORECASE)
        if match is None:
            raise ValueError("campaign file must open with a 'Subject: <text>' line")
        subject = match.group(1).strip()
        body_start = index + 1
        break
    if subject is None:
        raise ValueError("campaign file is empty")
    if not subject:
        raise ValueError("campaign subject is empty")
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        raise ValueError("campaign body is empty")
    return {"subject_template": subject, "body_template": body}


def personalize(template: str, contact: dict[str, Any]) -> tuple[str, list[str]]:
    """Substitute ``{placeholder}`` tokens in ``template`` from a contact.

    Returns the rendered text plus the list of personalization gaps -- one
    entry per token whose contact field was NULL (the fallback was used) or
    per token with no known mapping (left verbatim). ``{company}`` reads the
    contact's ``company_domain``.
    """
    gaps: list[str] = []

    def field_for(token: str) -> str | None:
        if token == "company":
            return contact.get("company_domain")
        if token == "full_name":
            parts = [contact.get("first_name"), contact.get("last_name")]
            joined = " ".join(p for p in parts if p)
            return joined or None
        return contact.get(token)

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in PLACEHOLDER_FALLBACKS:
            gaps.append(f"unknown placeholder {{{token}}}")
            return match.group(0)
        value = field_for(token)
        if value is None or str(value).strip() == "":
            gaps.append(f"missing {token}")
            return PLACEHOLDER_FALLBACKS[token]
        return str(value)

    rendered = PLACEHOLDER_RE.sub(replace, template)
    return rendered, gaps


def make_subject(run_id: str, sequence: int, rendered_subject: str) -> str:
    """Prefix a rendered subject with the run/sequence correlation tag."""
    return f"[{SUBJECT_TAG_PREFIX} {run_id} {sequence}] {rendered_subject}"


def mp(args: list[str], *, check: bool = True) -> dict[str, Any]:
    """Run a ``mailpilot`` subcommand and return its parsed JSON stdout.

    Args:
        args: CLI arguments after ``mailpilot`` (e.g. ``["contact", "list"]``).
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
