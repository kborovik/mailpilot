"""Workflow template registry contract tests.

Universal invariants are parametrized over every template in the registry;
named tests assert per-template tool / protocol contracts that protect
against the bugs §V.44 / §V.45 / §V.46 prevent (drive-tool over-binding,
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
        """§V.46: name = <direction>-<data-system>; prefix must match direction."""
        assert template.name.startswith(f"{template.direction}-"), (
            f"template name {template.name!r} prefix does not match "
            f"direction {template.direction!r}"
        )

    def test_description_non_empty(self, template: WorkflowTemplate) -> None:
        assert template.description.strip() != ""

    def test_decline_protocol_present(self, template: WorkflowTemplate) -> None:
        """§V.45: _DECLINE fragment composed into every template's protocol."""
        assert "polite decline" in template.protocol

    def test_deferred_task_protocol_present(self, template: WorkflowTemplate) -> None:
        """§V.45: _DEFERRED_TASK fragment composed into every template's protocol."""
        assert "record_enrollment_outcome" in template.protocol

    def test_no_fabrication_protocol_present(self, template: WorkflowTemplate) -> None:
        """§V.45: _NO_FABRICATION fragment composed into every template's protocol."""
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
    """§B.10 regression: outbound workflow must not bind any Drive tool."""
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
    """§V.41: KB grounding rule lives only in inbound-google-drive protocol."""
    protocol = TEMPLATES["inbound-google-drive"].protocol
    assert "search_drive_markdown" in protocol
    assert "read_drive_markdown" in protocol


def test_inbound_google_drive_protocol_carries_search_budget_fallback() -> None:
    """§V.41(+): after two consecutive search_drive_markdown misses the agent
    must fall back to list_drive_markdown rather than spin further searches."""
    protocol = TEMPLATES["inbound-google-drive"].protocol
    assert "two consecutive search_drive_markdown" in protocol
    assert "list_drive_markdown once instead" in protocol
    assert "do not retry search_drive_markdown a third time" in protocol
    # Regression: the existing >=3-reads-on-top-hits rule survives the amend.
    assert "top >=3 results" in protocol


def test_inbound_google_drive_protocol_carries_per_target_compare_rule() -> None:
    """§V.41(+): compare-and-contrast replies (>=2 distinct product targets)
    must issue >=1 target-specific search_drive_markdown per distinct target
    before list_drive_markdown is acceptable as grounding for that target,
    even when other targets are already grounded by earlier searches.

    §V.45: the rule lives in the _DRIVE_GROUNDING overlay bound only to
    inbound-google-drive -- non-Drive templates physically cannot trigger
    the compare path."""
    protocol = TEMPLATES["inbound-google-drive"].protocol
    assert "compare-and-contrast" in protocol
    assert "per target" in protocol
    assert "each distinct target" in protocol
    # Non-Drive templates must not pick up the per-target rule.
    for name in ("outbound-general", "inbound-general"):
        assert "compare-and-contrast" not in TEMPLATES[name].protocol


def test_inbound_google_drive_drive_tools_marked_sequential() -> None:
    """§V.38 + §B.34: every Drive Tool binding must carry ``sequential=True``.

    The underlying ``httplib2.Http`` transport has no internal locks, so an
    Anthropic-emitted parallel fan-out used to race the connection-pool dict
    (one read returned in ~1s while its sibling hung 60s at the socket
    timeout, killing the agent run). ``sequential=True`` tells the Pydantic
    AI dispatcher to serialize parallel emissions on these tools; this
    contract test catches a regression at registration (someone drops the
    kwarg, or a new non-thread-safe tool lands without it)."""
    drive_tool_names = {
        "list_drive_markdown",
        "read_drive_markdown",
        "search_drive_markdown",
    }
    for tool in TEMPLATES["inbound-google-drive"].tools:
        if tool.name in drive_tool_names:
            assert tool.sequential is True, (
                f"Drive tool {tool.name!r} must register with sequential=True "
                f"per §V.38 -- httplib2.Http is not thread-safe"
            )


def test_non_drive_templates_protocol_excludes_grounding() -> None:
    """§V.45: _DRIVE_GROUNDING bound only to inbound-google-drive."""
    for name in ("outbound-general", "inbound-general"):
        protocol = TEMPLATES[name].protocol
        assert "search_drive_markdown" not in protocol
        assert "read_drive_markdown" not in protocol


# -- §V.31: trigger-aware deferred-task fragment -------------------------------


_RECORD_OUTCOME_INSTRUCTION = (
    "After completing the workflow objective for a contact, "
    "call record_enrollment_outcome"
)
_INITIAL_INSTRUCTION = "Send the initial email and stop"


@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
def test_build_protocol_task_carries_record_outcome_instruction(
    template: WorkflowTemplate,
) -> None:
    """§V.31: ``trigger='task'`` keeps the terminal-outcome instruction."""
    protocol = template.build_protocol("task")
    assert _RECORD_OUTCOME_INSTRUCTION in protocol
    assert _INITIAL_INSTRUCTION not in protocol


@pytest.mark.parametrize(
    "trigger",
    ["enrollment_run", "enrollment_schedule", "manual", "email", "anything_else"],
)
@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
def test_build_protocol_non_task_uses_initial_branch(
    template: WorkflowTemplate, trigger: str
) -> None:
    """§V.31 + §V.32: non-``task`` triggers swap to the initial-send-only
    instruction. ``enrollment_schedule`` joins the non-task set because it
    shares first-touch semantics with ``enrollment_run`` per §V.30."""
    protocol = template.build_protocol(trigger)
    assert _INITIAL_INSTRUCTION in protocol
    assert _RECORD_OUTCOME_INSTRUCTION not in protocol


@pytest.mark.parametrize(
    "trigger", ["task", "enrollment_run", "enrollment_schedule", "manual"]
)
@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
def test_build_protocol_preserves_v33_fragment_order(
    template: WorkflowTemplate, trigger: str
) -> None:
    """§V.45: canonical order _BASE -> deferred -> [overlay]? -> _DECLINE ->
    _NO_FABRICATION preserved across triggers."""
    protocol = template.build_protocol(trigger)
    base_idx = protocol.find("Keep your final summary brief")
    decline_idx = protocol.find("polite decline")
    nofab_idx = protocol.find("Never fabricate")
    assert 0 <= base_idx < decline_idx < nofab_idx, (
        f"template {template.name!r} trigger={trigger!r}: §V.45 order broken "
        f"(base={base_idx}, decline={decline_idx}, nofab={nofab_idx})"
    )
    if template.name == "inbound-google-drive":
        grounding_idx = protocol.find("Workflow instructions reference a Google Drive")
        assert 0 < grounding_idx < decline_idx, (
            "§V.45: _DRIVE_GROUNDING must precede _DECLINE in inbound-google-drive"
        )


def test_protocol_property_returns_task_branch() -> None:
    """``WorkflowTemplate.protocol`` property mirrors ``build_protocol('task')``
    so ``mailpilot template view`` and existing CLI consumers stay on the
    terminal-outcome composition by default."""
    for template in TEMPLATES.values():
        assert template.protocol == template.build_protocol("task")


def test_outbound_general_direction() -> None:
    assert TEMPLATES["outbound-general"].direction == "outbound"


def test_inbound_general_direction() -> None:
    assert TEMPLATES["inbound-general"].direction == "inbound"


def test_inbound_google_drive_direction() -> None:
    assert TEMPLATES["inbound-google-drive"].direction == "inbound"


def test_template_dataclass_is_frozen() -> None:
    """§V.44: WorkflowTemplate immutability prevents runtime mutation."""
    template = TEMPLATES["outbound-general"]
    with pytest.raises((AttributeError, TypeError)):
        template.name = "other"  # type: ignore[misc]


# -- §V.40: ≥2 tool names per example sequence ---------------------------------

import re  # noqa: E402

from mailpilot.agent import templates as templates_module  # noqa: E402


def _known_tool_names() -> set[str]:
    """Union of every tool name bound to any template in the registry."""
    names: set[str] = set()
    for template in TEMPLATES.values():
        names.update(tool.name for tool in template.tools)
    return names


def _fragment_constants() -> dict[str, str]:
    """Return all module-level _UPPER_SNAKE str constants in templates.py.

    These are the protocol fragments composed into template protocols
    per §V.45. Picking them up by naming convention keeps the test honest
    if a new fragment lands without an explicit registry entry.
    """
    return {
        name: value
        for name, value in vars(templates_module).items()
        if name.startswith("_") and name[1:].isupper() and isinstance(value, str)
    }


@pytest.mark.parametrize(
    ("fragment_name", "fragment"),
    list(_fragment_constants().items()),
    ids=list(_fragment_constants().keys()),
)
def test_fragment_does_not_collapse_to_single_tool_example(
    fragment_name: str, fragment: str
) -> None:
    """§V.40: any fragment that names a tool must name >=2 distinct tools.

    A single-tool example collapses to literal -- the agent emits exactly
    that one tool name and fails to generalise. Either no tool names, or
    >=2 distinct names; never exactly 1.
    """
    known = _known_tool_names()
    mentioned = {
        name for name in known if re.search(rf"\b{re.escape(name)}\b", fragment)
    }
    assert len(mentioned) != 1, (
        f"fragment {fragment_name!r} names exactly 1 tool ({mentioned!r}); "
        f"§V.40 requires either 0 or >=2 distinct tool names per fragment"
    )


# -- _build_agent integration --------------------------------------------------


@pytest.mark.parametrize("template_name", list(TEMPLATES.keys()))
def test_build_agent_binds_template_tools(template_name: str) -> None:
    """§V.45: _build_agent binds exactly the template's tools and protocol."""
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
        account_email="owner@example.com",
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

    # Protocol prefix + workflow instructions concatenated. _build_agent
    # defaults to ``trigger="manual"`` per §V.31 -> initial-mode protocol.
    instructions_list = agent._instructions  # pyright: ignore[reportPrivateUsage]
    assert isinstance(instructions_list, list)
    str_parts = [item for item in instructions_list if isinstance(item, str)]
    instructions = "".join(str_parts)
    assert instructions.startswith(template.build_protocol("manual"))
    assert instructions.endswith("WORKFLOW-SPECIFIC-INSTRUCTIONS")


@pytest.mark.parametrize("template_name", list(TEMPLATES.keys()))
def test_build_agent_trigger_routes_protocol_branch(template_name: str) -> None:
    """§V.31: ``_build_agent`` swaps the deferred-task branch per ``trigger``.

    ``trigger='task'`` keeps the terminal-outcome instruction; non-``task``
    triggers swap to the initial-send-only branch so the agent's system
    prompt cannot direct premature ``record_enrollment_outcome`` calls
    on first reach-out.
    """
    from datetime import UTC, datetime

    from mailpilot.agent.invoke import (
        _build_agent,  # pyright: ignore[reportPrivateUsage]
    )
    from mailpilot.models import Workflow

    template = TEMPLATES[template_name]  # type: ignore[index]
    now = datetime.now(UTC)
    workflow = Workflow(
        id="01900000-0000-7000-8000-000000000003",
        name="W",
        template=template.name,
        type=template.direction,
        account_id="01900000-0000-7000-8000-000000000004",
        account_email="owner@example.com",
        status="active",
        instructions="",
        created_at=now,
        updated_at=now,
    )

    def _instructions(trigger: str) -> str:
        agent = _build_agent(workflow, trigger=trigger)
        parts = agent._instructions  # pyright: ignore[reportPrivateUsage]
        assert isinstance(parts, list)
        return "".join(item for item in parts if isinstance(item, str))

    task_instructions = _instructions("task")
    assert _RECORD_OUTCOME_INSTRUCTION in task_instructions
    assert _INITIAL_INSTRUCTION not in task_instructions

    for trigger in ("enrollment_run", "enrollment_schedule", "manual", "email"):
        initial_instructions = _instructions(trigger)
        assert _INITIAL_INSTRUCTION in initial_instructions
        assert _RECORD_OUTCOME_INSTRUCTION not in initial_instructions
