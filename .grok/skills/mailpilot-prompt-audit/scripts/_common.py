"""Shared helpers for the mailpilot-prompt-audit skill scripts.

Deterministic, no-LLM utilities: locate the repo and per-run artifact dir,
open a read-only database connection, load settings, read/write the compact
JSON envelopes the orchestrator passes between phases, and estimate token
counts from character length.

These scripts never send Gmail, never start ``mailpilot run``, and never
mutate the database -- they only read workflow rows and compose the prompt
text the agent and classifier already run. GitHub issue create is owned by
the orchestrator, not these scripts. Run every script via ``uv run python``
so the project venv (and the importable ``mailpilot`` package) is on PATH.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """Return the repository root by walking up for ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def add_src_to_path() -> None:
    """Make ``mailpilot`` importable even if the venv is not the active one."""
    src = repo_root() / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def run_dir(run_id: str) -> Path:
    """Return (creating) the artifact dir for a run: ``<repo>/reports/prompt-audit/<run_id>``."""
    directory = repo_root() / "reports" / "prompt-audit" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_json(path: Path) -> Any:
    """Parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as indented JSON (datetimes coerced to str)."""
    path.write_text(json.dumps(obj, indent=2, default=str))


def approx_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for budgeting, not billing."""
    return (len(text) + 3) // 4


def open_connection() -> Any:
    """Open a read-only-by-use database connection from settings.

    ``initialize_database`` provisions an empty DB but never mutates a
    populated one as a connection side-effect (§V.110), so this is safe for an
    audit. The caller owns closing the connection.
    """
    add_src_to_path()
    from mailpilot.database import initialize_database
    from mailpilot.settings import get_settings

    settings = get_settings()
    return initialize_database(str(settings.database_url))
