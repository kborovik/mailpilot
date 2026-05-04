"""Workflow template registry contract tests.

Universal invariants are parametrized over every template in the registry;
named tests assert per-template tool / protocol contracts that protect
against the bugs §V.32 / §V.33 / §V.34 prevent (drive-tool over-binding,
KB grounding rule leak, abstract naming).
"""

from __future__ import annotations

from typing import get_args

import pytest

from mailpilot.agent.templates import (
    TEMPLATES,
    WorkflowTemplate,
    WorkflowTemplateName,
    template_names,
)


def test_template_registry_keys_match_literal() -> None:
    """TEMPLATES keys must equal WorkflowTemplateName Literal members."""
    assert set(TEMPLATES.keys()) == set(get_args(WorkflowTemplateName))


def test_template_names_helper_returns_literal_args() -> None:
    """template_names() returns all Literal members."""
    assert set(template_names()) == set(get_args(WorkflowTemplateName))


@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
class TestUniversalTemplateInvariants:
    """Rules that hold for every template in the registry."""

    def test_protocol_non_empty(self, template: WorkflowTemplate) -> None:
        assert template.protocol.strip() != ""

    def test_tools_non_empty(self, template: WorkflowTemplate) -> None:
        assert len(template.tools) > 0

    def test_name_prefix_matches_direction(self, template: WorkflowTemplate) -> None:
        """V34: name = <direction>-<data-system>; prefix must match direction."""
        assert template.name.startswith(f"{template.direction}-"), (
            f"template name {template.name!r} prefix does not match "
            f"direction {template.direction!r}"
        )

    def test_description_non_empty(self, template: WorkflowTemplate) -> None:
        assert template.description.strip() != ""

    def test_decline_protocol_present(self, template: WorkflowTemplate) -> None:
        """V33: _DECLINE fragment composed into every template's protocol."""
        assert "polite decline" in template.protocol

    def test_deferred_task_protocol_present(self, template: WorkflowTemplate) -> None:
        """V33: _DEFERRED_TASK fragment composed into every template's protocol."""
        assert "record_enrollment_outcome" in template.protocol

    def test_no_fabrication_protocol_present(self, template: WorkflowTemplate) -> None:
        """V33: _NO_FABRICATION fragment composed into every template's protocol."""
        assert "fabricate" in template.protocol

    def test_protocol_warns_about_redundant_read_email(
        self, template: WorkflowTemplate
    ) -> None:
        """Trigger email body is inlined in the user prompt -- protocol must
        tell the agent not to waste a round-trip on read_email."""
        assert "read_email" in template.protocol

    def test_protocol_does_not_prohibit_markdown(
        self, template: WorkflowTemplate
    ) -> None:
        """Email bodies may use Markdown; protocol must not prohibit it."""
        assert "No markdown" not in template.protocol


# -- Per-template contract -----------------------------------------------------


def _tool_names(template: WorkflowTemplate) -> set[str]:
    return {tool.name for tool in template.tools}


def test_outbound_general_excludes_drive_tools() -> None:
    """B10 regression: outbound workflow must not bind any Drive tool."""
    names = _tool_names(TEMPLATES["outbound-general"])
    assert "list_drive_markdown" not in names
    assert "read_drive_markdown" not in names
    assert "search_drive_markdown" not in names


def test_inbound_general_excludes_drive_tools() -> None:
    """Inbound without KB must not bind any Drive tool."""
    names = _tool_names(TEMPLATES["inbound-general"])
    assert "list_drive_markdown" not in names
    assert "read_drive_markdown" not in names
    assert "search_drive_markdown" not in names


def test_inbound_google_drive_includes_drive_tools() -> None:
    """KB-grounded template must bind all 3 Drive tools."""
    names = _tool_names(TEMPLATES["inbound-google-drive"])
    assert "list_drive_markdown" in names
    assert "read_drive_markdown" in names
    assert "search_drive_markdown" in names


def test_inbound_google_drive_protocol_carries_grounding() -> None:
    """V28: KB grounding rule lives only in inbound-google-drive protocol."""
    protocol = TEMPLATES["inbound-google-drive"].protocol
    assert "search_drive_markdown" in protocol
    assert "read_drive_markdown" in protocol


def test_non_drive_templates_protocol_excludes_grounding() -> None:
    """V33: _DRIVE_GROUNDING bound only to inbound-google-drive."""
    for name in ("outbound-general", "inbound-general"):
        protocol = TEMPLATES[name].protocol
        assert "search_drive_markdown" not in protocol
        assert "read_drive_markdown" not in protocol


def test_outbound_general_direction() -> None:
    assert TEMPLATES["outbound-general"].direction == "outbound"


def test_inbound_general_direction() -> None:
    assert TEMPLATES["inbound-general"].direction == "inbound"


def test_inbound_google_drive_direction() -> None:
    assert TEMPLATES["inbound-google-drive"].direction == "inbound"


def test_template_dataclass_is_frozen() -> None:
    """V32: WorkflowTemplate immutability prevents runtime mutation."""
    template = TEMPLATES["outbound-general"]
    with pytest.raises((AttributeError, TypeError)):
        template.name = "other"  # type: ignore[misc]


# -- _build_agent integration --------------------------------------------------


@pytest.mark.parametrize("template_name", list(TEMPLATES.keys()))
def test_build_agent_binds_template_tools(template_name: str) -> None:
    """V33: _build_agent binds exactly the template's tools and protocol."""
    from datetime import UTC, datetime

    from mailpilot.agent.invoke import (
        _build_agent,  # pyright: ignore[reportPrivateUsage]
    )
    from mailpilot.models import Workflow

    template = TEMPLATES[template_name]  # type: ignore[index]
    now = datetime.now(UTC)
    workflow = Workflow(
        id="01900000-0000-7000-8000-000000000001",
        name="W",
        template=template.name,
        type=template.direction,
        account_id="01900000-0000-7000-8000-000000000002",
        status="active",
        instructions="WORKFLOW-SPECIFIC-INSTRUCTIONS",
        created_at=now,
        updated_at=now,
    )

    agent = _build_agent(workflow)

    # Tool name set matches template exactly.
    bound_names = {tool.name for tool in agent._function_toolset.tools.values()}  # pyright: ignore[reportPrivateUsage]
    expected = {tool.name for tool in template.tools}
    assert bound_names == expected, (
        f"template {template.name!r}: expected {expected}, got {bound_names}"
    )

    # Protocol prefix + workflow instructions concatenated.
    instructions_list = agent._instructions  # pyright: ignore[reportPrivateUsage]
    assert isinstance(instructions_list, list)
    str_parts = [item for item in instructions_list if isinstance(item, str)]
    instructions = "".join(str_parts)
    assert instructions.startswith(template.protocol)
    assert instructions.endswith("WORKFLOW-SPECIFIC-INSTRUCTIONS")
