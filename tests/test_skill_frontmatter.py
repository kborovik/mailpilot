"""§V.102 project-skill frontmatter hygiene — zero-body-use grant banned."""

from __future__ import annotations

import re
from pathlib import Path

_PROMPT_AUDIT = (
    Path(__file__).resolve().parents[1]
    / ".grok"
    / "skills"
    / "mailpilot-prompt-audit"
    / "SKILL.md"
)

_ALLOWED_TOOLS = re.compile(r"^allowed-tools:\s*(.+)$", re.M)


def _allowed_tools(path: Path) -> list[str]:
    match = _ALLOWED_TOOLS.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path} missing allowed-tools"
    return [token.strip() for token in match.group(1).split(",") if token.strip()]


def test_prompt_audit_allowed_tools_omits_search_tool() -> None:
    """§V.102 / §B.149: orchestrator never invokes search_tool."""
    tools = _allowed_tools(_PROMPT_AUDIT)
    assert "search_tool" not in tools
    assert "read_file" in tools
    assert "run_terminal_command" in tools
    assert "spawn_subagent" in tools
