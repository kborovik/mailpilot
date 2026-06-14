"""§V.73(d) drift-guard -- each skill-body embedded Workflow snippet's
post-``meta`` body is byte-identical to its saved ``.claude/workflows/*.js``.

A ``.claude/skills/<skill>/SKILL.md`` embeds the full Workflow body below
``meta`` as the spec-of-record mirror (self-contained, runnable as authored),
but the saved ``.claude/workflows/<name>.js`` is the file invoked BY NAME at
runtime. The saved ``meta`` legitimately adds registry-only fields
(``whenToUse``, a fuller ``description``), so only the post-``meta`` body is
compared. Silent divergence there ships an unaudited workflow -- the skill body
reads correct while the runtime runs something else. This test mechanizes the
check-extras §V.73(d) recipe so drift fails CI, not review.

Mirrors ``tests/test_exception_pairing_sweep.py``: the real parametrized
assertion plus non-vacuous guards (the matcher must actually extract bodies,
the comparator must actually detect divergence, and ``PAIRS`` must cover every
spec-of-record mirror in the tree).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_SKILLS_ROOT = _REPO_ROOT / ".claude" / "skills"

# Each (skill SKILL.md, saved workflow) pair whose post-`meta` bodies must match.
# Extend this list when a new skill+workflow spec-of-record mirror lands -- the
# completeness guard below fails until it is added.
_PAIRS = (
    (
        _SKILLS_ROOT / "lead-companies" / "SKILL.md",
        _REPO_ROOT / ".claude" / "workflows" / "lead-companies-enrich.js",
    ),
    (
        _SKILLS_ROOT / "lead-contacts" / "SKILL.md",
        _REPO_ROOT / ".claude" / "workflows" / "lead-contacts-find.js",
    ),
)

_FIRST_JS_BLOCK = re.compile(r"```js\n(.*?)```", re.DOTALL)


def _first_js_block(skill_markdown: str) -> str:
    """Return the first fenced ``js`` block body from a SKILL.md (the mirror)."""
    match = _FIRST_JS_BLOCK.search(skill_markdown)
    if match is None:
        raise AssertionError("no fenced ```js block found in skill body")
    return match.group(1)


def _post_meta_body(source: str) -> str:
    """Slice a Workflow source to everything after its ``meta`` literal.

    Cuts at the first ``\\n}\\n`` following ``export const meta`` (the closing
    brace of the ``meta`` object literal) and strips surrounding whitespace, so
    registry-only ``meta`` field differences never register as drift.
    """
    meta_start = source.find("export const meta")
    if meta_start < 0:
        raise AssertionError("source does not declare `export const meta`")
    meta_close = source.find("\n}\n", meta_start)
    if meta_close < 0:
        raise AssertionError("source has no `meta` object closing `\\n}\\n`")
    return source[meta_close + 3 :].strip()


@pytest.mark.parametrize(
    ("skill_path", "saved_path"),
    _PAIRS,
    ids=[saved_path.name for _, saved_path in _PAIRS],
)
def test_skill_mirror_matches_saved_workflow(
    skill_path: Path, saved_path: Path
) -> None:
    """§V.73(d): the skill-body mirror's post-``meta`` body is byte-identical to
    the saved workflow's post-``meta`` body."""
    embedded_body = _post_meta_body(_first_js_block(skill_path.read_text()))
    saved_body = _post_meta_body(saved_path.read_text())

    assert embedded_body == saved_body, (
        f"{skill_path.relative_to(_REPO_ROOT)} spec-of-record mirror has drifted "
        f"from saved {saved_path.relative_to(_REPO_ROOT)} (post-`meta` body); the "
        "saved file is invoked by name at runtime so the divergence ships an "
        "unaudited workflow (§V.73(d)). Re-sync the two bodies."
    )


def test_pairs_cover_every_spec_of_record_mirror() -> None:
    """Completeness guard: every ``spec-of-record mirror`` SKILL.md is in
    ``_PAIRS``, so a new skill+workflow mirror cannot ship unaudited."""
    mirror_skills = {
        skill_md
        for skill_md in _SKILLS_ROOT.glob("**/SKILL.md")
        if "spec-of-record mirror" in skill_md.read_text()
    }
    covered = {skill_path for skill_path, _ in _PAIRS}

    assert mirror_skills == covered, (
        "every SKILL.md carrying a `spec-of-record mirror` must be byte-identity "
        f"tested; uncovered={sorted(str(p) for p in mirror_skills - covered)} "
        f"stale={sorted(str(p) for p in covered - mirror_skills)} -- extend _PAIRS "
        "(§V.73(d))"
    )


def test_extraction_is_non_vacuous() -> None:
    """Guard against a silently-broken matcher -- each embedded mirror must
    start with the ``meta`` declaration and yield a non-empty post-``meta``
    body, else the identity test would pass over nothing."""
    for skill_path, _ in _PAIRS:
        block = _first_js_block(skill_path.read_text())
        assert block.lstrip().startswith("export const meta"), (
            f"{skill_path}: first ```js block is not the meta-led mirror"
        )
        assert _post_meta_body(block), f"{skill_path}: empty post-`meta` body"


def test_comparator_detects_divergence() -> None:
    """Guard against a tautological assertion -- the comparison must flag a
    body that differs by a single token, not just whitespace."""
    skill_path, saved_path = _PAIRS[0]
    saved_body = _post_meta_body(saved_path.read_text())
    mutated = saved_body.replace("parallel", "pipeline", 1)

    assert mutated != saved_body, "mutation produced no change -- guard is vacuous"
    assert _post_meta_body(_first_js_block(skill_path.read_text())) != mutated
