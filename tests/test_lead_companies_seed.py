"""Tests for the lead-companies seed script `seed_companies.py` (SPEC §V.98 / §T.115).

`seed_companies.py` lives in the lead-companies skill, not the importable
`mailpilot` package, so it is loaded via `importlib.util` from its on-disk path
(mirrors `tests/test_smoke_qa.py`).

§V.98: a resolved-apex create collision is recorded in the run-summary
`collapsed` accumulator when the incoming CSV display name diverges from the
owning company's name (a hidden entity merge); a same-name re-seed stays a
silent `existing`. The collision fires both intra-batch and onto a
previously-seeded row.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

SEED_PY_PATH = (
    Path(__file__).parent.parent
    / ".claude"
    / "skills"
    / "lead-companies"
    / "scripts"
    / "seed_companies.py"
)


@pytest.fixture
def seed_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("seed_under_test", SEED_PY_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_names_diverge_empty_incoming_is_silent(seed_module: types.ModuleType) -> None:
    # No incoming CSV display name (plain-text/inline domain) -> no merge signal.
    assert seed_module.names_diverge("", "White Cap") is False


def test_names_diverge_same_name_normalized(seed_module: types.ModuleType) -> None:
    # Case + surrounding whitespace differences are not an entity merge.
    assert seed_module.names_diverge("  white cap ", "White Cap") is False


def test_names_diverge_distinct_entity(seed_module: types.ModuleType) -> None:
    assert (
        seed_module.names_diverge("National Concrete Accessories", "White Cap") is True
    )


def test_divergent_reseed_records_collapsed_not_existing(
    seed_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §B.75 recurrence: a CSV row ("National Concrete Accessories") redirecting
    # onto a previously-seeded apex owned by "White Cap" must surface as a
    # collapsed entity merge, never a bare `existing`.
    monkeypatch.setattr(seed_module, "resolve_apex", lambda _apex: "whitecapsupply.com")
    monkeypatch.setattr(
        seed_module,
        "create_company",
        lambda domain, _name, _dry_run: ("existing", {"domain": domain}),
    )
    monkeypatch.setattr(seed_module, "fetch_owner_name", lambda _domain: "White Cap")

    result = seed_module.seed(
        [("nca.com", "National Concrete Accessories")], dry_run=False
    )

    assert "whitecapsupply.com" not in result["existing"]
    assert result["collapsed"] == [
        {
            "resolved": "whitecapsupply.com",
            "owner_name": "White Cap",
            "incoming_names": ["National Concrete Accessories"],
        }
    ]


def test_same_name_reseed_stays_silent_existing(
    seed_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "resolve_apex", lambda _apex: "whitecapsupply.com")
    monkeypatch.setattr(
        seed_module,
        "create_company",
        lambda domain, _name, _dry_run: ("existing", {"domain": domain}),
    )
    monkeypatch.setattr(seed_module, "fetch_owner_name", lambda _domain: "White Cap")

    result = seed_module.seed([("whitecapsupply.com", "White Cap")], dry_run=False)

    assert result["existing"] == ["whitecapsupply.com"]
    assert result["collapsed"] == []


def test_intra_batch_divergent_merge_records_collapsed(
    seed_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two distinct-name CSV rows redirecting onto one apex in the same run: the
    # first creates the row, the second collapses onto it (§V.98 intra-batch).
    monkeypatch.setattr(seed_module, "resolve_apex", lambda _apex: "whitecapsupply.com")
    monkeypatch.setattr(
        seed_module,
        "create_company",
        lambda _domain, _name, _dry_run: ("created", {"id": "id-1"}),
    )

    result = seed_module.seed(
        [
            ("whitecapsupply.com", "White Cap"),
            ("nca.com", "National Concrete Accessories"),
        ],
        dry_run=False,
    )

    assert result["created"] == ["id-1"]
    assert result["existing"] == []
    assert result["collapsed"] == [
        {
            "resolved": "whitecapsupply.com",
            "owner_name": "White Cap",
            "incoming_names": ["National Concrete Accessories"],
        }
    ]


def test_seed_reports_touched_apexes(
    seed_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `touched_apexes` is the scope key main() uses to build `seeded_stale`: it
    # must list every resolved apex handled this run, created or duplicate-key.
    monkeypatch.setattr(seed_module, "resolve_apex", lambda apex: apex)
    monkeypatch.setattr(
        seed_module,
        "create_company",
        lambda domain, _name, _dry_run: ("created", {"id": f"id-{domain}"}),
    )

    result = seed_module.seed([("acme.com", ""), ("beta.io", "")], dry_run=False)

    assert result["touched_apexes"] == ["acme.com", "beta.io"]


def test_scope_stale_to_seeded_drops_untouched_backlog(
    seed_module: types.ModuleType,
) -> None:
    # A domain-token run must enrich only rows it touched, never the whole
    # `profile IS NULL` backlog (Pipeline table: "stale scoped to those rows").
    stale = [
        {"id": "id-acme", "domain": "acme.com", "name": "Acme"},
        {"id": "id-old1", "domain": "legacy-one.com", "name": "Legacy One"},
        {"id": "id-old2", "domain": "legacy-two.com", "name": "Legacy Two"},
    ]

    scoped = seed_module.scope_stale_to_seeded(
        stale, created_ids={"id-acme"}, touched_apexes=set()
    )

    assert scoped == [{"id": "id-acme", "domain": "acme.com", "name": "Acme"}]


def test_scope_stale_to_seeded_matches_reseed_by_domain(
    seed_module: types.ModuleType,
) -> None:
    # A duplicate-key re-seed produces no new id, so the row is reachable only
    # via its resolved apex in `touched_apexes` -- it must still be enriched.
    stale = [{"id": "id-wc", "domain": "whitecapsupply.com", "name": "White Cap"}]

    scoped = seed_module.scope_stale_to_seeded(
        stale, created_ids=set(), touched_apexes={"whitecapsupply.com"}
    )

    assert scoped == stale
