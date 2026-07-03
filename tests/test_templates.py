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
        """§V.31 / §V.45: a deferred-task branch composes into every template's
        protocol -- the outbound terminal-outcome branch (names
        ``conclude_enrollment``) for outbound, the inbound-reply branch for
        inbound (names no tool, reply once then stop)."""
        if template.direction == "inbound":
            assert _INBOUND_INSTRUCTION in template.protocol
        else:
            assert "conclude_enrollment" in template.protocol

    def test_no_fabrication_protocol_present(self, template: WorkflowTemplate) -> None:
        """§V.45: _NO_FABRICATION fragment composed into every template's protocol."""
        assert "fabricate" in template.protocol

    def test_protocol_guards_against_redundant_trigger_email_fetch(
        self, template: WorkflowTemplate
    ) -> None:
        """§V.135: the trigger email body is inlined in the user prompt, so the
        protocol tells the agent it is already provided -- yet it names no read
        tool (naming exactly one read tool would trip §V.40, and the CRM records
        are now mechanically pre-fed rather than fetched)."""
        protocol = template.protocol
        assert "already provided" in protocol
        for read_tool in ("read_email", "read_contact", "read_company"):
            assert read_tool not in protocol, (
                f"template {template.name!r}: §V.135 -- _BASE must name no read "
                f"tool, but the composed protocol mentions {read_tool!r}"
            )

    def test_protocol_does_not_prohibit_markdown(
        self, template: WorkflowTemplate
    ) -> None:
        """Email bodies may use Markdown; protocol must not prohibit it."""
        assert "No markdown" not in template.protocol


def test_spec_table_mandates_pipe_table_for_spec_rows() -> None:
    """§V.42 / §B.78 / §B.114: the GFM pipe-table mandate for product-spec rows
    lives in the _SPEC_TABLE fragment so the format lint is a backstop, not the
    primary enforcement. A permissive "may use Markdown tables" alone is the bug
    §B.78 records -- the agent emits a space-aligned spec block first, the lint
    rejects, the agent retries (7/8 invocations under burst, retry_rate 0.875 >>
    the §V.70 0.05 ceiling). Product-spec answering is an inbound knowledge-base
    concern (§B.114), so the fragment composes into the inbound templates only."""
    spec_table = templates_module._SPEC_TABLE  # pyright: ignore[reportPrivateUsage]
    # Marked as a hard requirement, not a permissive suggestion.
    assert "MUST" in spec_table
    # The mandated rendering is a pipe table with a |---| header separator.
    assert "pipe table" in spec_table
    assert "|---|" in spec_table
    # Scoped to product-spec rows per §V.42 (model numbers, flow rates, ...).
    assert "model numbers" in spec_table
    # The mandate flows into the inbound templates only (§V.42, §V.45).
    for name in ("inbound-general", "inbound-google-drive"):
        assert "pipe table" in TEMPLATES[name].protocol
        assert "|---|" in TEMPLATES[name].protocol


def test_outbound_excludes_spec_table_mandate() -> None:
    """§V.42 / §V.45 / §B.114: the product-spec pipe-table mandate is an inbound
    knowledge-base concern, so outbound-general carries none of it -- its
    protocol_pre is _BASE alone, and the composed protocol names no product-spec
    / pipe-table / flow-rate text. Outbound scaffolding stays on-topic."""
    outbound = TEMPLATES["outbound-general"]
    assert outbound.protocol_pre == templates_module._BASE  # pyright: ignore[reportPrivateUsage]
    protocol = outbound.protocol
    for marker in ("pipe table", "|---|", "product specification", "flow rate"):
        assert marker not in protocol, (
            f"§B.114: product-spec marker {marker!r} leaked into the "
            f"outbound-general protocol -- the _SPEC_TABLE mandate is inbound-only"
        )


def test_base_strips_permissive_markdown_wording() -> None:
    """§V.42 / §B.83: the permissive "may use Markdown" line must be gone from
    _BASE. T128 added the pipe-table mandate but left the permissive wording
    beside it; the agent reads the soft phrasing first, emits a space-aligned
    spec block, the lint rejects, and it retries (the §B.78 retry class §B.83
    re-opened). The check-extras §V.42 recipe greps for this exact phrase, so
    the mandate must stand alone."""
    base = templates_module._BASE  # pyright: ignore[reportPrivateUsage]
    assert "may use Markdown" not in base
    for template in TEMPLATES.values():
        assert "may use Markdown" not in template.protocol


def test_base_names_no_read_tools() -> None:
    """§V.135 / §V.40: _BASE names no read tool.

    The contact + company records are mechanically pre-fed into the user prompt
    (§V.135) rather than fetched, so _BASE no longer references read_contact /
    read_company, and the trigger-email nudge no longer names read_email.
    Naming exactly one read tool would trip §V.40 (a fragment names 0 or >=2
    tools); _BASE names zero. The personalization directive still stands, now
    pointed at the pre-fed records."""
    base = templates_module._BASE  # pyright: ignore[reportPrivateUsage]
    for read_tool in ("read_email", "read_contact", "read_company"):
        assert read_tool not in base
    # The behavioural guidance survives the reword.
    assert "already provided" in base
    assert "records" in base


# -- Per-template contract -----------------------------------------------------


def _tool_names(template: WorkflowTemplate) -> set[str]:
    return {tool.name for tool in template.tools}


def test_record_enrollment_outcome_absent_from_agent_tool_set() -> None:
    """§V.127 / §V.31 / §I: ``record_enrollment_outcome`` is the system-internal
    recorder (§V.15) -- it is never bound to any template's tool set.

    ``conclude_enrollment`` is the outbound terminal, bound to outbound
    templates. Inbound templates bind neither ``conclude_enrollment`` nor
    ``create_task`` (§V.31) -- the system records the inbound outcome and an
    inbound reply schedules no follow-up (§B.124).
    """
    for template in TEMPLATES.values():
        names = _tool_names(template)
        assert "record_enrollment_outcome" not in names, (
            f"template {template.name!r} still binds record_enrollment_outcome; "
            f"§V.127 makes it system-internal"
        )
        if template.direction == "outbound":
            assert "conclude_enrollment" in names, (
                f"outbound template {template.name!r} must bind "
                f"conclude_enrollment (§V.127)"
            )
        else:
            assert "conclude_enrollment" not in names, (
                f"inbound template {template.name!r} must not bind "
                f"conclude_enrollment (§V.31)"
            )
            assert "create_task" not in names, (
                f"inbound template {template.name!r} must not bind create_task (§V.31)"
            )


def test_inbound_templates_exclude_lifecycle_tools_keep_send_tools() -> None:
    """§V.31 / §B.124: inbound templates bind neither ``conclude_enrollment``
    nor ``create_task`` -- the system records the inbound outcome and an inbound
    reply schedules no follow-up -- yet they keep the send + read tools an
    inbound auto-reply needs (reply_email, send_email, noop, read_email,
    search_emails). The CRM record tools (read_contact / read_company) are gone
    from every roster -- those records are mechanically pre-fed (§V.135)."""
    for name in ("inbound-general", "inbound-google-drive"):
        names = _tool_names(TEMPLATES[name])
        assert "conclude_enrollment" not in names
        assert "create_task" not in names
        for kept in (
            "reply_email",
            "send_email",
            "noop",
            "read_email",
            "search_emails",
        ):
            assert kept in names, (
                f"inbound template {name!r} dropped {kept!r} -- §V.31 removes "
                f"only conclude_enrollment + create_task from _CORE"
            )
        # §V.135: the CRM record lookups are off every roster (pre-fed instead).
        assert "read_contact" not in names
        assert "read_company" not in names


def test_outbound_general_binds_lifecycle_tools() -> None:
    """§V.31 / §V.127: the outbound template keeps the full lifecycle roster --
    ``conclude_enrollment`` (terminal) and ``create_task`` (deferred follow-up)
    -- because outbound sequences own their enrollment lifecycle."""
    names = _tool_names(TEMPLATES["outbound-general"])
    assert "conclude_enrollment" in names
    assert "create_task" in names


def test_inbound_deferred_fragment_reply_once_records_outcome() -> None:
    """§V.31 / §B.124: the inbound deferred fragment tells the agent to reply
    once and stop, states the system records the outcome, and names no tool
    (§V.40: a fragment names 0 or >=2 tools -- inbound's structural guard is the
    tool roster, so the fragment names none). It carries no SPEC cite (§V.45)
    and is ASCII-only (§C)."""
    fragment = templates_module._DEFERRED_TASK_INBOUND  # pyright: ignore[reportPrivateUsage]
    assert _INBOUND_INSTRUCTION in fragment
    assert "records the" in fragment
    # The two outbound-lifecycle tool names must not appear -- the inbound
    # fragment neither orders nor forbids a tool it cannot call.
    assert "conclude_enrollment" not in fragment
    assert "create_task" not in fragment
    assert fragment.isascii()
    assert _SPEC_CITE.search(fragment) is None


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


def test_inbound_google_drive_binds_drive_tools_without_grounding_fragment() -> None:
    """§V.41 / §V.45: inbound-google-drive binds the Drive tool set but its
    protocol carries NO grounding fragment. The KB-grounding discipline
    (search-first, 2-search budget, read top >=3, per-target compare search)
    and the verbatim-citation / no-unit-conversion guidance are
    workflow-specific behaviour -- they live in workflow.instructions
    (workflows/*.toml per §V.103), never a code-defined template fragment.

    The proof is byte-level: with the grounding overlay removed, the Drive
    template's protocol_post is identical to the non-Drive templates' (just
    _DECLINE + _NO_FABRICATION); the only thing that distinguishes the Drive
    template is its bound tool set."""
    drive = TEMPLATES["inbound-google-drive"]
    # Still binds all three Drive tools (§V.41: "binds the Drive tool set").
    expected_drive = {
        "list_drive_markdown",
        "read_drive_markdown",
        "search_drive_markdown",
    }
    assert expected_drive <= _tool_names(drive)
    # No grounding fragment: protocol_post is identical to the fragment-free
    # templates -- just _DECLINE + _NO_FABRICATION, no overlay.
    assert drive.protocol_post == TEMPLATES["inbound-general"].protocol_post
    assert drive.protocol_post == TEMPLATES["outbound-general"].protocol_post
    # The grounding discipline markers must not leak into the composed protocol.
    protocol = drive.protocol
    for marker in (
        "search_drive_markdown",
        "read_drive_markdown",
        "two consecutive",
        "compare-and-contrast",
        "verbatim as published",
        "Do not convert units",
    ):
        assert marker not in protocol, (
            f"§V.41: grounding marker {marker!r} leaked into the "
            f"inbound-google-drive template protocol -- it belongs in "
            f"workflow.instructions"
        )


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


_CONCLUDE_INSTRUCTION = (
    "After achieving the workflow goal for a contact, conclude the enrollment"
)
_INBOUND_INSTRUCTION = "Reply to the inbound email once, then stop"


@pytest.mark.parametrize(
    "trigger",
    ["task", "enrollment_run", "enrollment_schedule", "manual", "email"],
)
@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
def test_build_protocol_branch_is_direction_only(
    template: WorkflowTemplate, trigger: str
) -> None:
    """§V.31 + §V.136: ``build_protocol`` is direction-only after the
    initial-send fragment was retired -- the outbound first reach-out is now a
    compose-only touch run (§V.136), so no trigger selects an initial-send
    branch. For every trigger an outbound template composes the terminal-outcome
    instruction and an inbound template the inbound-reply instruction (§B.124:
    the old TASK branch used to order a ``conclude_enrollment`` inbound workflows
    forbid; the old INITIAL branch mislabeled an inbound reply as first
    reach-out)."""
    protocol = template.build_protocol(trigger)
    if template.direction == "inbound":
        assert _INBOUND_INSTRUCTION in protocol
        assert _CONCLUDE_INSTRUCTION not in protocol
    else:
        assert _CONCLUDE_INSTRUCTION in protocol
        assert _INBOUND_INSTRUCTION not in protocol
    # The retired initial-send fragment never appears in a composed protocol.
    assert "Send the initial email and stop" not in protocol


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


# -- §V.45 / §V.120: _MUST_SEND terminal-reply fragment ------------------------


def test_must_send_fragment_present_in_every_template() -> None:
    """§V.45 / §V.120: _MUST_SEND is the prompt-side mirror of the §V.120
    runtime reply guard. It instructs the model to end every trigger turn in a
    real reply_email / send_email tool call or an explicit noop -- drafting the
    reply in reasoning is not sending. It composes into every template's
    protocol across every trigger, and it lives as a template fragment (not
    workflow.instructions) because must-send is direction-universal mechanics,
    not workflow-specific policy."""
    must_send = templates_module._MUST_SEND  # pyright: ignore[reportPrivateUsage]
    # Names both send paths + the explicit-decline escape (>=2 tools per §V.40).
    assert "reply_email" in must_send
    assert "send_email" in must_send
    assert "noop" in must_send
    for template in TEMPLATES.values():
        for trigger in _ALL_TRIGGERS:
            assert must_send in template.build_protocol(trigger), (
                f"template {template.name!r} trigger={trigger!r}: _MUST_SEND "
                f"fragment missing from composed protocol"
            )


@pytest.mark.parametrize(
    "trigger", ["task", "enrollment_run", "enrollment_schedule", "manual", "email"]
)
@pytest.mark.parametrize("template", list(TEMPLATES.values()), ids=lambda t: t.name)
def test_must_send_ordered_after_trigger_branch_before_decline(
    template: WorkflowTemplate, trigger: str
) -> None:
    """§V.45: canonical fragment order _BASE -> trigger branch -> _MUST_SEND ->
    _DECLINE -> _NO_FABRICATION. _MUST_SEND sits after the deferred-task branch
    and before _DECLINE in the composed protocol."""
    protocol = template.build_protocol(trigger)
    must_send = templates_module._MUST_SEND  # pyright: ignore[reportPrivateUsage]
    # §V.136: the deferred branch is direction-only -- inbound-reply for inbound,
    # terminal-outcome for outbound (the initial-send fragment was retired).
    deferred_marker = (
        _INBOUND_INSTRUCTION
        if template.direction == "inbound"
        else _CONCLUDE_INSTRUCTION
    )
    base_idx = protocol.find("Keep your final summary brief")
    deferred_idx = protocol.find(deferred_marker)
    must_send_idx = protocol.find(must_send)
    decline_idx = protocol.find("polite decline")
    nofab_idx = protocol.find("Never fabricate")
    assert 0 <= base_idx < deferred_idx < must_send_idx < decline_idx < nofab_idx, (
        f"template {template.name!r} trigger={trigger!r}: §V.45 order broken "
        f"(base={base_idx}, deferred={deferred_idx}, must_send={must_send_idx}, "
        f"decline={decline_idx}, nofab={nofab_idx})"
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
from mailpilot.agent import tools as agent_tools_module  # noqa: E402


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


# -- §V.45: no SPEC citation in agent-visible text -----------------------------


_SPEC_CITE = re.compile(r"§[VTB]\.[0-9]+")
_ALL_TRIGGERS = ("task", "enrollment_run", "enrollment_schedule", "manual", "email")


def test_composed_protocol_carries_no_spec_citation() -> None:
    """§V.45 / §B.79: the runtime reply agent has no SPEC.md, so a literal
    ``§V/§T/§B.<n>`` token in any composed protocol is dead authoring metadata
    leaking into the system prompt. The governing invariant is cited in an
    adjacent code comment instead -- e.g. the §V.42 pipe-table mandate keeps its
    "rejected by the outbound format lint" motivation but drops the numbering.
    Sweep every template across every trigger branch."""
    for template in TEMPLATES.values():
        for trigger in _ALL_TRIGGERS:
            protocol = template.build_protocol(trigger)
            match = _SPEC_CITE.search(protocol)
            assert match is None, (
                f"template {template.name!r} trigger={trigger!r}: composed "
                f"protocol embeds SPEC cite {match.group()!r} -- §V.45 forbids "
                f"§-numbering in agent-visible text (cite it in a comment)"
            )


def test_registered_tool_descriptions_carry_no_spec_citation() -> None:
    """§V.45: a tool's model-visible description (derived from its docstring)
    must not embed a ``§V/§T/§B.<n>`` token either -- the agent sees tool
    descriptions in the same prompt context as the protocol. Sweep every tool
    bound to any template."""
    for template in TEMPLATES.values():
        for tool in template.tools:
            description = tool.description or ""
            match = _SPEC_CITE.search(description)
            assert match is None, (
                f"template {template.name!r} tool {tool.name!r}: description "
                f"embeds SPEC cite {match.group()!r} -- §V.45 forbids "
                f"§-numbering in model-visible tool descriptions"
            )


def test_registered_tool_source_docstrings_carry_no_spec_citation() -> None:
    """§V.45 / §B.84: pydantic-ai derives a tool's full model-visible schema --
    description AND per-parameter help -- from the registered function's
    docstring, including the Args/Returns sections. T130's guard scanned only
    ``tool.description`` (the wrapper summary), so §-cites buried in an Args or
    Returns line of a source-function docstring (create_task, conclude_enrollment,
    list_enrollments, read_email, read_drive_markdown)
    leaked to the model unaudited. Sweep the full source docstring of every
    registered tool. Internal helpers (_check_spec_table) are out of scope --
    they are never registered, so the model never sees them."""
    for template in TEMPLATES.values():
        for tool in template.tools:
            source_fn = getattr(agent_tools_module, tool.name)
            docstring = source_fn.__doc__ or ""
            match = _SPEC_CITE.search(docstring)
            assert match is None, (
                f"template {template.name!r} tool {tool.name!r}: source "
                f"docstring embeds SPEC cite {match.group()!r} -- §V.45 forbids "
                f"§-numbering in model-visible tool descriptions"
            )


# -- §V.131: fixed fallback acknowledgement body ------------------------------


def test_fallback_acknowledgement_is_fixed_content_free_ascii() -> None:
    """§V.131: the fallback acknowledgement is a code-defined, content-free,
    ASCII body -- never model-generated and never partial -- so the grounding
    risk the agent failed on cannot reach the sender (§B.116). It carries no
    SPEC cite (§C ASCII-only project artifact) and is not composed into any
    template protocol (it is an email body, not a prompt fragment)."""
    body = templates_module._FALLBACK_ACKNOWLEDGEMENT  # pyright: ignore[reportPrivateUsage]
    assert isinstance(body, str)
    assert body.strip()
    assert body.isascii()
    assert _SPEC_CITE.search(body) is None
    for template in TEMPLATES.values():
        for trigger in _ALL_TRIGGERS:
            assert body not in template.build_protocol(trigger)


# -- §V.136: compose-only touch protocol fragment ------------------------------


def test_touch_compose_fragment_hygiene() -> None:
    """§V.136 / §V.45 / §V.40: the compose-only touch protocol is a non-empty,
    ASCII, SPEC-cite-free fragment that names no tool (the compose-only agent
    binds none, so §V.40 does not apply) and is NOT composed into any tool-loop
    template protocol -- it is the separate compose-only shape (§V.44)."""
    fragment = templates_module._TOUCH_COMPOSE  # pyright: ignore[reportPrivateUsage]
    assert isinstance(fragment, str)
    assert fragment.strip()
    assert fragment.isascii()
    assert _SPEC_CITE.search(fragment) is None
    # Names no bound tool (structured output is the action, no tool call).
    known = _known_tool_names()
    mentioned = {
        name for name in known if re.search(rf"\b{re.escape(name)}\b", fragment)
    }
    assert mentioned == set()
    # Not part of any tool-loop protocol composition.
    for template in TEMPLATES.values():
        for trigger in _ALL_TRIGGERS:
            assert fragment not in template.build_protocol(trigger)


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
    """§V.31 + §V.136: ``_build_agent`` composes the direction-only deferred
    branch.

    An inbound template uses the inbound-reply branch (reply once, then stop);
    an outbound template uses the terminal-outcome branch, for every trigger.
    The initial-send-only branch was retired -- the outbound first reach-out is
    a compose-only touch run (§V.136), not a tool-loop protocol.
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

    for trigger in ("task", "enrollment_run", "enrollment_schedule", "manual", "email"):
        instructions = _instructions(trigger)
        if template.direction == "inbound":
            assert _INBOUND_INSTRUCTION in instructions
            assert _CONCLUDE_INSTRUCTION not in instructions
        else:
            assert _CONCLUDE_INSTRUCTION in instructions
            assert _INBOUND_INSTRUCTION not in instructions
        assert "Send the initial email and stop" not in instructions
