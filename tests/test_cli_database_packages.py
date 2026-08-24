"""§V.190 / §V.2: cli/ and database/ package split invariants."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mailpilot"
CLI_DIR = SRC / "cli"
DATABASE_DIR = SRC / "database"

_HEAVY_MODULES = frozenset(
    {
        "logfire",
        "psycopg",
        "httpx",
        "pydantic",
        "mailpilot.database",
        "mailpilot.settings",
        "googleapiclient",
        "google.auth",
        "mailpilot.agent",
        "mailpilot.sync",
        "mailpilot.run",
    }
)
_ALLOWED_MAILPILOT = frozenset({"mailpilot._filters", "mailpilot.cli"})


def _module_level_imported(tree: ast.Module) -> set[str]:
    """Return top-level imported module names (skip TYPE_CHECKING blocks)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
            names.add(node.module)
    return names


def test_database_public_imports_stable() -> None:
    """§V.190: callers keep `from mailpilot.database import X`."""
    from mailpilot.database import (
        create_company,
        get_status_payload,
        initialize_database,
        list_accounts,
    )

    assert callable(initialize_database)
    assert callable(create_company)
    assert callable(list_accounts)
    assert callable(get_status_payload)


def test_database_is_package() -> None:
    """§V.190: database/ package with schema + per-entity modules."""
    assert DATABASE_DIR.is_dir()
    assert (DATABASE_DIR / "__init__.py").is_file()
    assert (DATABASE_DIR / "schema.py").is_file()
    assert (DATABASE_DIR / "status.py").is_file()
    for noun in (
        "account",
        "company",
        "contact",
        "email",
        "workflow",
        "enrollment",
        "task",
        "tag",
        "note",
        "activity",
        "meeting",
    ):
        assert (DATABASE_DIR / f"{noun}.py").is_file(), noun


def test_cli_is_package_with_main_and_nouns() -> None:
    """§V.190: cli/main.py holds group; per-noun modules hold commands."""
    assert CLI_DIR.is_dir()
    assert (CLI_DIR / "__init__.py").is_file()
    assert (CLI_DIR / "main.py").is_file()
    for noun in (
        "show",
        "db",
        "config",
        "account",
        "company",
        "contact",
        "email",
        "activity",
        "tag",
        "note",
        "workflow",
        "template",
        "enrollment",
        "task",
        "meeting",
    ):
        assert (CLI_DIR / f"{noun}.py").is_file(), noun
    source = (CLI_DIR / "main.py").read_text(encoding="utf-8")
    assert "def main(" in source
    assert "def _db(" in source
    assert "def _resolve" in source
    assert "def output(" in source


def test_cli_modules_lazy_import_heavy_deps() -> None:
    """§V.2: CLI package module-level imports are click / stdlib / _filters."""
    for path in sorted(CLI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert isinstance(tree, ast.Module)
        imported = _module_level_imported(tree)
        heavy = []
        for name in imported:
            if name in _HEAVY_MODULES or name.startswith("mailpilot.database"):
                heavy.append(name)
                continue
            if (
                name.startswith("mailpilot.")
                and not any(
                    name == allowed or name.startswith(f"{allowed}.")
                    for allowed in _ALLOWED_MAILPILOT
                )
                and name not in {"mailpilot._filters"}
            ):
                heavy.append(name)
        assert not heavy, f"{path.name} module-level heavy imports: {heavy}"


def test_cli_help_stays_fast_without_database() -> None:
    """§V.2 / §V.190: `mailpilot --help` does not import mailpilot.database."""
    probe = r"""
import sys
from click.testing import CliRunner
from mailpilot.cli import main
result = CliRunner().invoke(main, ["--help"])
assert result.exit_code == 0, result.output
assert "mailpilot CLI skill" in result.output
assert "mailpilot.database" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_entrypoint_unchanged() -> None:
    """§V.190 / I.entrypoint: pyproject still points at mailpilot.cli:main."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'mailpilot = "mailpilot.cli:main"' in text
    from mailpilot.cli import main

    assert callable(main)


def test_cli_package_reexports_main() -> None:
    """Entrypoint `mailpilot.cli:main` resolves through the package."""
    cli = importlib.import_module("mailpilot.cli")
    assert hasattr(cli, "main")
    assert cli.main.name == "main" or callable(cli.main)
