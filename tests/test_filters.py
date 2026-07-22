"""Unit tests for the shared ``list`` filter decorators (§V.115)."""

from __future__ import annotations

import click
from click.testing import CliRunner

from mailpilot._filters import (
    COMPANY_SORT_KEYS,
    DIRECTIONS,
    desc_option,
    enum_option,
    include_disabled_option,
    limit_option,
    offset_option,
    presence_option,
    range_options,
    scope_option,
    sort_option,
    time_window_options,
)


def _params(cmd: click.Command) -> dict[str, click.Parameter]:
    return {p.name: p for p in cmd.params if p.name is not None}


def test_limit_option_defaults_to_100() -> None:
    @click.command()
    @limit_option
    def cmd(limit: int) -> None:
        click.echo(str(limit))

    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "100"


def test_limit_option_custom_default() -> None:
    @click.command()
    @limit_option(default=500)
    def cmd(limit: int) -> None:
        click.echo(str(limit))

    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "500"


def test_offset_option_defaults_to_0() -> None:
    @click.command()
    @offset_option
    def cmd(offset: int) -> None:
        click.echo(str(offset))

    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "0"


def test_sort_option_choice_and_default() -> None:
    @click.command()
    @sort_option(COMPANY_SORT_KEYS, default="name")
    def cmd(sort: str) -> None:
        click.echo(sort)

    ok = CliRunner().invoke(cmd, [])
    assert ok.exit_code == 0
    assert ok.output.strip() == "name"
    key = CliRunner().invoke(cmd, ["--sort", "domain"])
    assert key.exit_code == 0
    assert key.output.strip() == "domain"
    bad = CliRunner().invoke(cmd, ["--sort", "nope"])
    assert bad.exit_code != 0


def test_desc_option_flag() -> None:
    @click.command()
    @desc_option
    def cmd(desc: bool) -> None:
        click.echo(str(desc))

    off = CliRunner().invoke(cmd, [])
    assert off.output.strip() == "False"
    on = CliRunner().invoke(cmd, ["--desc"])
    assert on.output.strip() == "True"


def test_time_window_options_adds_since_and_until() -> None:
    @click.command()
    @time_window_options("created_at")
    def cmd(since: str | None, until: str | None) -> None:
        click.echo(f"{since}|{until}")

    params = _params(cmd)
    assert "since" in params
    assert "until" in params
    result = CliRunner().invoke(cmd, ["--since", "a", "--until", "b"])
    assert result.output.strip() == "a|b"


def test_scope_option_declares_named_flag() -> None:
    @click.command()
    @scope_option("--company-id", "company_id", "Filter by company ID.")
    def cmd(company_id: str | None) -> None:
        click.echo(str(company_id))

    result = CliRunner().invoke(cmd, ["--company-id", "X"])
    assert result.output.strip() == "X"


def test_enum_option_rejects_out_of_set_value() -> None:
    @click.command()
    @enum_option("--status", "status", ["a", "b"], "Filter by status.")
    def cmd(status: str | None) -> None:
        click.echo(str(status))

    ok = CliRunner().invoke(cmd, ["--status", "a"])
    assert ok.exit_code == 0
    bad = CliRunner().invoke(cmd, ["--status", "z"])
    assert bad.exit_code != 0


def test_range_options_min_and_max_inclusive_ints() -> None:
    @click.command()
    @range_options("contacts", "lower bound", "upper bound")
    def cmd(min_contacts: int | None, max_contacts: int | None) -> None:
        click.echo(f"{min_contacts}|{max_contacts}")

    result = CliRunner().invoke(cmd, ["--min-contacts", "1", "--max-contacts", "5"])
    assert result.output.strip() == "1|5"


def test_range_options_field_with_dash_maps_to_underscore_param() -> None:
    @click.command()
    @range_options("email-confidence", "lower", "upper")
    def cmd(min_email_confidence: int | None, max_email_confidence: int | None) -> None:
        click.echo(f"{min_email_confidence}|{max_email_confidence}")

    result = CliRunner().invoke(
        cmd, ["--min-email-confidence", "10", "--max-email-confidence", "90"]
    )
    assert result.output.strip() == "10|90"


def test_presence_option_is_tristate_without_no_has_artifact() -> None:
    @click.command()
    @presence_option("profile", "Presence of profile.")
    def cmd(has_profile: bool | None) -> None:
        click.echo(repr(has_profile))

    none_state = CliRunner().invoke(cmd, [])
    assert none_state.output.strip() == "None"
    true_state = CliRunner().invoke(cmd, ["--has-profile"])
    assert true_state.output.strip() == "True"
    false_state = CliRunner().invoke(cmd, ["--no-profile"])
    assert false_state.output.strip() == "False"
    # No `--no-has-profile` artifact is generated.
    bogus = CliRunner().invoke(cmd, ["--no-has-profile"])
    assert bogus.exit_code != 0


def test_include_disabled_flag_defaults_false() -> None:
    @click.command()
    @include_disabled_option
    def cmd(include_disabled: bool) -> None:
        click.echo(repr(include_disabled))

    off = CliRunner().invoke(cmd, [])
    assert off.output.strip() == "False"
    on = CliRunner().invoke(cmd, ["--include-disabled"])
    assert on.output.strip() == "True"


def test_directions_axis_is_inbound_outbound() -> None:
    assert DIRECTIONS == ["inbound", "outbound"]
