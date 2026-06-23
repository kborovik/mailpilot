"""§V.119 guard: a destructive DB op backs up the database snapshot first.

§V.119 requires ``make db-backup`` run one ``mailpilot db export`` to
``~/Documents/MailPilot/`` as a timestamped JSON snapshot (§V.121), writing its
file and exiting 0 *before* any ``dropdb``. A failed export must abort the drop
(fail-closed, no ``|| true`` swallow), so the paid live company + contact rows
(discovery credits, §V.96/§V.113) are never wiped without a durable backup that
lives outside the dropped database.

These tests mechanize that invariant over the ``Makefile``:

- ``db-backup`` exists and runs one ``mailpilot db export`` (not the removed
  per-entity ``company``/``contact export``) to ``$(DOCUMENTS_DIR)`` =
  ``$(HOME)/Documents/MailPilot`` as timestamped ``.json``.
- the backup path carries no ``|| true`` (fail-closed).
- ``clean`` depends on ``db-backup`` and its own recipe carries no export, so
  the only export path is the prerequisite -- it runs before ``dropdb``.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_MAKEFILE = _REPO_ROOT / "Makefile"


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


def test_db_backup_target_exists_and_exports_db_snapshot() -> None:
    """§V.119/§V.121: ``db-backup`` runs one ``db export`` snapshot."""
    text = _MAKEFILE.read_text()
    recipe = _target_recipe(text, "db-backup")
    assert recipe, "Makefile must define a `db-backup` target (§V.119)"
    assert "db export" in recipe, (
        "db-backup must run `mailpilot db export` (§V.119/§V.121)"
    )
    assert "company export" not in recipe, (
        "per-entity `company export` is removed -- the snapshot is `db export` (§V.121)"
    )
    assert "contact export" not in recipe, (
        "per-entity `contact export` is removed -- the snapshot is `db export` (§V.121)"
    )


def test_db_backup_writes_timestamped_json_under_documents_mailpilot() -> None:
    """§V.119/§V.63: backups are timestamped JSON under ~/Documents/MailPilot/."""
    text = _MAKEFILE.read_text()
    assert re.search(
        r"^DOCUMENTS_DIR\s*:?=\s*\$\(HOME\)/Documents/MailPilot\s*$",
        text,
        re.MULTILINE,
    ), "Makefile must define DOCUMENTS_DIR := $(HOME)/Documents/MailPilot (§V.119)"
    recipe = _target_recipe(text, "db-backup")
    assert "$(DOCUMENTS_DIR)" in recipe, (
        "db-backup must write into $(DOCUMENTS_DIR) = ~/Documents/MailPilot/ (§V.119)"
    )
    assert ".json" in recipe, "backups must be JSON (§V.63)"
    assert "$(TS)" in recipe, "backup filenames must carry a timestamp (§V.119)"


def test_db_backup_is_fail_closed() -> None:
    """§V.119: the backup path swallows no failure -- no `|| true`."""
    recipe = _target_recipe(_MAKEFILE.read_text(), "db-backup")
    assert "|| true" not in recipe, (
        "db-backup must be fail-closed -- a failed export aborts the drop, "
        "never `|| true` (§V.119)"
    )


def test_fail_closed_detector_is_non_vacuous() -> None:
    """Guard against a tautological check -- reintroducing `|| true` must trip
    the fail-closed assertion."""
    regressed = "\tmailpilot db export --file x.json || true"
    assert "|| true" in regressed, "detector must catch a reintroduced `|| true`"


def test_clean_depends_on_db_backup_before_dropdb() -> None:
    """§V.119: ``clean`` depends on ``db-backup`` and runs the backup before any
    ``dropdb`` -- the export is the prerequisite, not an inline `|| true` line."""
    text = _MAKEFILE.read_text()
    assert "db-backup" in _prerequisites(text, "clean"), (
        "`clean` must declare `db-backup` as a prerequisite so the backup runs "
        "before dropdb (§V.119)"
    )
    clean_recipe = _target_recipe(text, "clean")
    assert "dropdb" in clean_recipe, "clean must still re-create the database"
    assert "db export" not in clean_recipe, (
        "clean must not export inline -- the only export path is the db-backup "
        "prerequisite, so a failed backup aborts the drop (§V.119)"
    )
    backup_recipe = _target_recipe(text, "db-backup")
    assert "dropdb" not in backup_recipe, (
        "db-backup must only back up, never drop -- the drop lives in clean, "
        "gated behind a successful backup (§V.119)"
    )
