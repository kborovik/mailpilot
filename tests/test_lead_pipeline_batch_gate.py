"""§V.117 guard -- the shared Batch-gate convention states the distinct-batch
option rule and suppresses the fixed `First 24` cap when stale-count <= 24.

`§B.98`: a fixed-cap option that equals `All <N>` at the current stale-count
dispatches one identical batch as `All <N>` -- two options, one outcome. §V.117
fixes this pre-cap (during option construction): every gate option MUST map to a
distinct batch at the current stale-count, so a fixed-cap option is dropped once
its cap reaches the stale-count (`First 24` dropped at stale-count <= 24, ==
`All <N>` there).

Per §V.100 the rule lives once in `references/lead-pipeline-conventions.md`
(both sibling skills cite it, neither copies it). This test mechanizes the
check-extras §V.117 grep so the rule cannot silently regress out of the
conventions file or get duplicated back into a SKILL body.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_SKILLS_ROOT = _REPO_ROOT / ".claude" / "skills"
_CONVENTIONS = (
    _SKILLS_ROOT / "lead-companies" / "references" / "lead-pipeline-conventions.md"
)
_SIBLING_SKILL_BODIES = (
    _SKILLS_ROOT / "lead-companies" / "SKILL.md",
    _SKILLS_ROOT / "lead-contacts" / "SKILL.md",
)

_BATCH_GATE_HEADING = "## Batch gate"


def _batch_gate_section(conventions_markdown: str) -> str:
    """Return the prose between the ``## Batch gate`` heading and the next ``##``."""
    start = conventions_markdown.find(_BATCH_GATE_HEADING)
    if start < 0:
        raise AssertionError("conventions file has no `## Batch gate` section")
    rest = conventions_markdown[start + len(_BATCH_GATE_HEADING) :]
    next_heading = re.search(r"\n## ", rest)
    return rest if next_heading is None else rest[: next_heading.start()]


def test_batch_gate_states_distinct_batch_rule() -> None:
    """§V.117: the Batch-gate § states each option maps to a distinct batch and
    suppresses `First 24` at stale-count <= 24 (== `All <N>` there)."""
    section = _batch_gate_section(_CONVENTIONS.read_text())

    assert "distinct batch" in section, (
        "Batch-gate § must state the distinct-batch option rule (§V.117)"
    )
    assert "First 24" in section, "Batch-gate § must list the `First 24` cap option"
    # The suppression condition: offer `First 24` only above its cap, drop it at/below.
    assert "stale-count > 24" in section, (
        "Batch-gate § must offer `First 24` only when stale-count > 24 (§V.117)"
    )
    assert "stale-count <= 24" in section, (
        "Batch-gate § must drop `First 24` when stale-count <= 24 (== `All <N>`, §V.117)"
    )


def test_first_nine_marked_always_distinct() -> None:
    """§V.117: `First 9` stays distinct because the gate fires only above 9."""
    section = _batch_gate_section(_CONVENTIONS.read_text())
    assert "First 9" in section, "Batch-gate § must list the `First 9` option"
    assert "stale-count > 9" in section, (
        "Batch-gate § must note the gate fires only at stale-count > 9, so "
        "`First 9` is always below the stale-count and stays distinct (§V.117)"
    )


def test_suppression_rule_not_duplicated_into_skill_bodies() -> None:
    """§V.100: the distinct-batch rule lives once in the conventions file; the
    sibling SKILL bodies cite it, they do not restate the suppression mechanics."""
    for skill_body in _SIBLING_SKILL_BODIES:
        text = skill_body.read_text()
        assert "stale-count <= 24" not in text, (
            f"{skill_body.relative_to(_REPO_ROOT)} restates the §V.117 suppression "
            "mechanics; the rule must live once in lead-pipeline-conventions.md (§V.100)"
        )


def test_assertion_is_non_vacuous() -> None:
    """Guard against a tautological check -- the markers must be absent from a
    Batch-gate § that still carries the pre-§V.117 unconditional option list."""
    pre_v117_section = (
        "## Batch gate\n\n"
        "- `len(<rows>) > 9` -> invoke `AskUserQuestion` with these options:\n"
        '  - `"First 9 (Recommended)"` -- cap to first 9\n'
        '  - `"First 24"` -- cap to first 24\n'
        '  - `"All <N>"` -- every stale row\n'
    )
    section = _batch_gate_section(pre_v117_section)
    assert "distinct batch" not in section
    assert "stale-count <= 24" not in section
