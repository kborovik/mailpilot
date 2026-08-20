"""Compose the prompt / LLM-agent shape for every system, deterministically.

Two LLM systems run in mailpilot, each composing its prompt differently:

- **Classifier** (``agent.classify_email`` span) -- one structured-output call,
  no tools. System prompt is the code constant ``_INSTRUCTIONS`` in
  ``agent/classify.py``; it routes an inbound email by matching each candidate
  workflow's ``goal``.
- **Workflow agent** (``agent.invoke`` span) -- the tool-using auto-reply /
  outbound agent. Its system prompt is ``template.build_protocol(trigger) +
  workflow.instructions`` (§V.44-45): code-defined protocol fragments from
  ``agent/templates.py`` followed by the workflow's own TOML ``instructions``.
  The deferred-task fragment is direction-scoped (§V.31 / §V.136): outbound
  tool-loop uses the terminal-outcome branch; inbound uses the reply-once
  branch; outbound first reach-out is compose-only (``_TOUCH_COMPOSE``). This
  script records the tool-loop prompt and, for outbound, the compose-only
  prompt.

The script reads live workflow rows from the database (their ids match the
Logfire ``workflow_id`` attribute, so telemetry joins back to a system) and
imports the code-defined prompt fragments, then writes ``composition.json``:
the full composed system-prompt text plus per-layer character / token sizes,
tagged by edit target (code fragment vs. workflow TOML). No telemetry here --
that join happens in ``analyze_prep.py``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from _common import add_src_to_path, approx_tokens, open_connection, run_dir, write_json


def _fragment_inventory() -> dict[str, dict[str, object]]:
    """Capture the code-defined protocol fragments shared across templates.

    Each fragment owns one project-wide rule (§V.45). Edits to these are a
    code change plus PR (§V.44), not a workflow update -- the report flags
    them accordingly.
    """
    from mailpilot.agent import templates as t

    named = {
        "_BASE": "brevity + trigger/contact reuse; records are pre-fed (§V.135)",
        "_PRODUCT_SPECS": "product-spec facts as list/prose (inbound protocol_pre only)",
        "_DEFERRED_TASK_TASK": "terminal disposition via conclude_enrollment (outbound tool-loop)",
        "_DEFERRED_TASK_INBOUND": "reply once then stop; system records outcome (inbound)",
        "_TOUCH_COMPOSE": "compose-only outbound touch; structured subject+body, no tools (§V.136)",
        "_MUST_SEND": "end every trigger turn in a real send or explicit noop (§V.120)",
        "_DECLINE": "polite decline without invented facts; still one tool call",
        "_NO_FABRICATION": "never fabricate specs / numbers / claims; decline when unsure",
        "_FALLBACK_ACKNOWLEDGEMENT": "fixed inbound-failure receipt body; model never sees this",
    }
    inventory: dict[str, dict[str, object]] = {}
    for name, rule in named.items():
        text = str(getattr(t, name))
        inventory[name] = {
            "text": text,
            "chars": len(text),
            "approx_tokens": approx_tokens(text),
            "rule": rule,
            "source": f"src/mailpilot/agent/templates.py:{name}",
            "edit_target": "code (templates.py) -- change + PR per §V.44",
        }
    return inventory


def _tool_contract() -> dict[str, object]:
    """Capture the conclude_enrollment disposition enum for tool-reference checks.

    The workflow agent's single terminal tool is ``conclude_enrollment`` (§V.127),
    and its ``disposition`` is the most common place a workflow instruction names a
    value the system does not accept. Shipping the enum lets the analysis verify a
    referenced tool and argument against the real palette instead of cross-reading
    the source.
    """
    from mailpilot.agent.tools import _CONCLUDE_DISPOSITIONS, _CONCLUDE_OUTCOME

    return {
        "conclude_enrollment": {
            "signature": "conclude_enrollment(disposition, note, reschedule_at=None)",
            "dispositions": list(_CONCLUDE_DISPOSITIONS),
            "disposition_outcome": dict(_CONCLUDE_OUTCOME),
            "source": "src/mailpilot/agent/tools.py:_CONCLUDE_DISPOSITIONS",
            "note": (
                "Only meeting_booked records a completed outcome; do_not_contact "
                "and contact_later record failed (do_not_contact also blocks the "
                "contact, contact_later schedules a re-enrollment touch). An "
                "instruction that names any other tool or disposition is a defect."
            ),
        }
    }


def _classifier_system() -> dict[str, object]:
    """Capture the classifier's code-defined system prompt and prompt shape."""
    from mailpilot.agent.classify import _INSTRUCTIONS, _MAX_BODY_CHARS

    return {
        "name": "classifier",
        "span": "agent.classify_email",
        "kind": "single-turn structured output, no tools",
        "system_prompt": _INSTRUCTIONS,
        "system_prompt_chars": len(_INSTRUCTIONS),
        "system_prompt_approx_tokens": approx_tokens(_INSTRUCTIONS),
        "source": "src/mailpilot/agent/classify.py:_INSTRUCTIONS",
        "edit_target": "code (classify.py) -- change + PR per §V.44",
        "routes_on": "each candidate workflow.goal (semantic match)",
        "user_prompt_shape": (
            "candidate workflows JSON (id, name, goal) + inbound email "
            f"From / Subject / body truncated to {_MAX_BODY_CHARS} chars"
        ),
    }


def _workflow_systems(
    connection: Any, statuses: list[str] | None
) -> list[dict[str, object]]:
    """Compose each live workflow agent's system prompt from template + TOML.

    ``statuses`` filters the workflow set (e.g. ``["active"]``). ``None``
    includes every status -- the full, honest inventory. Test-scaffold
    workflows left ``paused`` by the campaign-test skill are real rows, so the
    filter (not a name heuristic) is how the operator narrows to live systems.
    """
    from mailpilot.agent.templates import (
        _DEFERRED_TASK_INBOUND,
        _DEFERRED_TASK_TASK,
        _TOUCH_COMPOSE,
        TEMPLATES,
    )
    from mailpilot.database import get_workflow, list_workflows

    summaries = list_workflows(connection, limit=500)
    systems: list[dict[str, object]] = []
    for summary in summaries:
        if statuses is not None and summary.status not in statuses:
            continue
        workflow = get_workflow(connection, summary.id)
        if workflow is None:
            continue
        template = TEMPLATES.get(workflow.template)
        if template is None:
            continue

        # Direction-scoped tool-loop protocol (§V.31 / §V.136): trigger no
        # longer selects a fragment. Outbound first reach-out is compose-only
        # (_TOUCH_COMPOSE); inbound uses _DEFERRED_TASK_INBOUND.
        protocol_task = template.build_protocol("task")
        system_task = protocol_task + workflow.instructions
        if template.direction == "outbound":
            system_initial = _TOUCH_COMPOSE + workflow.instructions
            deferred_branch = _DEFERRED_TASK_TASK
            deferred_compose = _TOUCH_COMPOSE
        else:
            system_initial = system_task
            deferred_branch = _DEFERRED_TASK_INBOUND
            deferred_compose = _DEFERRED_TASK_INBOUND

        systems.append(
            {
                "workflow_id": workflow.id,
                "name": workflow.name,
                "template": workflow.template,
                "type": workflow.type,
                "status": workflow.status,
                "account_email": workflow.account_email,
                "theme": workflow.theme,
                "tools": [tool.name for tool in template.tools],
                # The two authored layers, sized and tagged by edit target.
                "goal": {
                    "text": workflow.goal,
                    "chars": len(workflow.goal),
                    "approx_tokens": approx_tokens(workflow.goal),
                    "edit_target": (
                        "workflows TOML -- goal (drives the classifier; "
                        f"workflow {workflow.name})"
                    ),
                },
                "instructions": {
                    "text": workflow.instructions,
                    "chars": len(workflow.instructions),
                    "approx_tokens": approx_tokens(workflow.instructions),
                    "edit_target": (
                        "workflows TOML -- instructions (operator-editable; "
                        f"workflow {workflow.name})"
                    ),
                },
                # Code-defined protocol wrapper (same for every workflow on the
                # template). Listed so the report can separate authored
                # instruction text from the shared protocol prefix / suffix.
                "protocol": {
                    "pre": template.protocol_pre,
                    "deferred_task": deferred_branch,
                    "deferred_initial": deferred_compose,
                    "post": template.protocol_post,
                    "edit_target": "code (templates.py) -- shared, change + PR per §V.44",
                },
                "composed_system_prompt_task": system_task,
                "composed_system_prompt_initial": system_initial,
                "sizes": {
                    "protocol_chars": len(protocol_task),
                    "instructions_chars": len(workflow.instructions),
                    "system_prompt_task_chars": len(system_task),
                    "system_prompt_task_approx_tokens": approx_tokens(system_task),
                    "system_prompt_initial_chars": len(system_initial),
                    "instruction_share": (
                        round(len(workflow.instructions) / len(system_task), 3)
                        if system_task
                        else 0.0
                    ),
                },
            }
        )
    return systems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--status",
        default="active",
        help=(
            "Comma-separated workflow statuses to include (default 'active'). "
            "Pass 'all' for the full inventory including paused/draft."
        ),
    )
    args = parser.parse_args()

    statuses: list[str] | None
    if args.status.strip().lower() == "all":
        statuses = None
    else:
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]

    add_src_to_path()
    connection = open_connection()
    try:
        workflow_systems = _workflow_systems(connection, statuses)
    finally:
        connection.close()

    classifier = _classifier_system()
    fragments = _fragment_inventory()
    tool_contract = _tool_contract()

    templates_used = sorted({str(s["template"]) for s in workflow_systems})
    total_instruction_chars = sum(
        int(s["instructions"]["chars"])
        for s in workflow_systems  # type: ignore[index]
    )

    composition = {
        "run_id": args.run_id,
        "status_filter": args.status,
        "systems_note": (
            "Two LLM systems: classifier (routes by workflow.goal) and the "
            "workflow agent (template protocol + workflow.instructions)."
        ),
        "classifier": classifier,
        "template_fragments": fragments,
        "tool_contract": tool_contract,
        "workflow_systems": workflow_systems,
        "summary": {
            "workflow_count": len(workflow_systems),
            "templates_used": templates_used,
            "total_instruction_chars": total_instruction_chars,
        },
    }

    write_json(run_dir(args.run_id) / "composition.json", composition)
    print(
        json.dumps(
            {
                "workflow_count": len(workflow_systems),
                "templates_used": templates_used,
                "classifier_system_prompt_chars": classifier["system_prompt_chars"],
                "fragment_count": len(fragments),
                "workflows": [
                    {
                        "workflow_id": s["workflow_id"],
                        "name": s["name"],
                        "template": s["template"],
                        "status": s["status"],
                        "instructions_chars": s["instructions"]["chars"],  # type: ignore[index]
                        "system_prompt_task_chars": s["sizes"][  # type: ignore[index]
                            "system_prompt_task_chars"
                        ],
                    }
                    for s in workflow_systems
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
