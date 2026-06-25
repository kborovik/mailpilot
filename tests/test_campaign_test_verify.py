"""Delivery-matching guards for the mailpilot-campaign-test verify script (§V.122).

The skill scripts live under ``.claude/skills/`` (outside the package roots), so
``verify_delivery`` is loaded by file path here rather than imported as a
package. Its ``compute_delivery`` is pure -- it takes the parsed ``sends.json``
and the ``email list`` rows and returns the delivery verdict -- so the alias-key
logic is tested without any live Gmail traffic. The script's ``_common`` import
is deferred into ``main()``, so loading the module top-level pulls in no sibling
``_common`` and cannot collide with the reply-test scripts' module of the same
name.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "mailpilot-campaign-test"
    / "scripts"
)


def _load(module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"campaign_test_{module_name}", _SCRIPTS / f"{module_name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _send(
    sequence: int, alias: str, subject: str, status: str = "sent"
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "alias": alias,
        "status": status,
        "subject": subject,
    }


def _row(*aliases: str, subject: str = "") -> dict[str, Any]:
    return {"subject": subject, "recipients": {"to": list(aliases)}}


def test_alias_match_marks_each_sequence_delivered() -> None:
    verify = _load("verify_delivery")
    sends = {
        "alias_mailbox": "inbound@lab5.ca",
        "sends": [
            _send(1, "inbound1@lab5.ca", "Subject one"),
            _send(2, "inbound2@lab5.ca", "Subject two"),
        ],
    }
    rows = [_row("inbound1@lab5.ca"), _row("inbound2@lab5.ca")]

    result = verify.compute_delivery(sends, rows)

    assert result["expected"] == [1, 2]
    assert result["delivered"] == [1, 2]
    assert result["missing"] == []


def test_two_sends_one_subject_both_delivered() -> None:
    """§V.122 / §B.108: a shared subject never collapses two arrivals.

    Sequences 2 and 3 carry the same agent-written subject but own distinct
    aliases. Both aliases arrive, so both count delivered -- the subject is not
    an identity key.
    """
    verify = _load("verify_delivery")
    shared_subject = "AI for customs inquiry triage and tariff lookups at GHY"
    sends = {
        "alias_mailbox": "inbound@lab5.ca",
        "sends": [
            _send(1, "inbound1@lab5.ca", "Distinct subject"),
            _send(2, "inbound2@lab5.ca", shared_subject),
            _send(3, "inbound3@lab5.ca", shared_subject),
        ],
    }
    rows = [
        _row("inbound1@lab5.ca", subject="Distinct subject"),
        _row("inbound2@lab5.ca", subject=shared_subject),
        _row("inbound3@lab5.ca", subject=shared_subject),
    ]

    result = verify.compute_delivery(sends, rows)

    assert result["delivered"] == [1, 2, 3]
    assert result["missing"] == []


def test_missing_alias_counts_missing() -> None:
    verify = _load("verify_delivery")
    sends = {
        "alias_mailbox": "inbound@lab5.ca",
        "sends": [
            _send(1, "inbound1@lab5.ca", "Subject one"),
            _send(2, "inbound2@lab5.ca", "Subject two"),
        ],
    }
    rows = [_row("inbound1@lab5.ca")]

    result = verify.compute_delivery(sends, rows)

    assert result["delivered"] == [1]
    assert result["missing"] == [2]


def test_recipient_alias_keys_over_subject() -> None:
    """An arrival is attributed by its alias even when its subject matches
    a different sequence's subject (subject is never the key)."""
    verify = _load("verify_delivery")
    sends = {
        "alias_mailbox": "inbound@lab5.ca",
        "sends": [
            _send(1, "inbound1@lab5.ca", "Subject A"),
            _send(2, "inbound2@lab5.ca", "Subject B"),
        ],
    }
    # The single arrival carries sequence 1's subject but sequence 2's alias.
    rows = [_row("inbound2@lab5.ca", subject="Subject A")]

    result = verify.compute_delivery(sends, rows)

    assert result["delivered"] == [2]
    assert result["missing"] == [1]


def test_skipped_and_failed_sends_excluded_from_expected() -> None:
    verify = _load("verify_delivery")
    sends = {
        "alias_mailbox": "inbound@lab5.ca",
        "sends": [
            _send(1, "inbound1@lab5.ca", "Sent", status="sent"),
            _send(2, "inbound2@lab5.ca", "Skipped", status="skipped"),
            _send(3, "inbound3@lab5.ca", "Failed", status="failed"),
        ],
    }
    rows = [_row("inbound1@lab5.ca")]

    result = verify.compute_delivery(sends, rows)

    assert result["expected"] == [1]
    assert result["delivered"] == [1]
    assert result["missing"] == []
