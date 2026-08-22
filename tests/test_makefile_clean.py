"""Guards for the ``clean`` makefile target.

``make clean`` is a deliberate wipe (§V.119): it does not auto-export, and
the retired ``db-backup`` / ``config-backup`` / ``env-backup`` targets stay
gone. After drop/create it restores empty ``app_config`` keys from
``pass mailpilot/``.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
# The tracked file is lowercase ``makefile`` (GNU make resolves it). A
# case-insensitive macOS filesystem aliases ``Makefile``, but a case-sensitive
# Linux CI checkout does not, so the literal must match the tracked case.
_MAKEFILE = _REPO_ROOT / "makefile"


def _target_recipe(makefile_text: str, target: str) -> str:
    """Return the recipe body (tab-indented lines) of a Makefile target.

    The recipe runs from the target's rule line to the first line that is
    neither tab-indented nor blank.
    """
    lines = makefile_text.splitlines()
    body: list[str] = []
    in_target = False
    for line in lines:
        if re.match(rf"^{re.escape(target)}\s*:", line):
            in_target = True
            continue
        if in_target:
            if line.startswith("\t"):
                body.append(line)
            elif line.strip() == "":
                continue
            else:
                break
    return "\n".join(body)


def _prerequisites(makefile_text: str, target: str) -> list[str]:
    """Return the prerequisite tokens declared on a target's rule line."""
    for line in makefile_text.splitlines():
        match = re.match(rf"^{re.escape(target)}\s*:([^=].*)$", line)
        if match:
            prereq_part = match.group(1).split("##")[0]
            return prereq_part.split()
    return []


def test_makefile_has_no_retired_backup_targets() -> None:
    """§V.119: db-backup, config-backup, and env-backup are gone."""
    text = _MAKEFILE.read_text()
    for target in ("db-backup", "config-backup", "env-backup"):
        assert not re.search(
            rf"^{re.escape(target)}\s*:",
            text,
            re.MULTILINE,
        ), f"makefile must not define `{target}` (§V.119)"
        assert target not in _prerequisites(text, "clean"), (
            f"`clean` must not depend on `{target}` (§V.119)"
        )


def test_clean_does_not_auto_export() -> None:
    """§V.119: clean is a deliberate wipe -- no inline db export."""
    recipe = _target_recipe(_MAKEFILE.read_text(), "clean")
    assert "dropdb" in recipe, "clean must still re-create the database"
    assert "db export" not in recipe, (
        "clean must not auto-export; snapshot is operator `mailpilot db export` "
        "(§V.119/§V.121)"
    )


def test_clean_restores_missing_config_from_pass_after_db_reset() -> None:
    """After drop/create, clean sets empty app_config keys from pass mailpilot/."""
    recipe = _target_recipe(_MAKEFILE.read_text(), "clean")
    assert "pass show" in recipe, (
        "clean must read values from `pass show mailpilot/<key>` after db reset"
    )
    assert "mailpilot/" in recipe, "pass prefix is mailpilot/"
    assert "config set" in recipe, (
        "clean must `mailpilot config set` missing keys from pass"
    )
    createdb_at = recipe.index("createdb mailpilot_test")
    pass_at = recipe.index("pass show")
    status_at = recipe.index("mailpilot status")
    assert createdb_at < pass_at < status_at, (
        "pass restore must run after both databases exist and before status"
    )
    for line in recipe.splitlines():
        if "pass show" in line or "config set" in line:
            assert "|| true" not in line, (
                "pass restore must be fail-closed -- a failed pass show or "
                "config set must abort clean"
            )
