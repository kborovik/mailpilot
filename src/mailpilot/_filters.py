"""Shared Click option decorators for ``list`` filter families.

Each ``list`` command composes its filters from a closed, fixed vocabulary of
six families, realized as the decorators below so that naming and semantics
stay uniform across all 11 list commands. A new list flag is either an existing
vocabulary decorator or a spec change -- never an ad-hoc ``@click.option``.

Families:

1. Scope -- ``scope_option`` (parent reference, validated in the command body).
2. Enum -- ``enum_option`` (``click.Choice`` mirroring a schema CHECK set).
3. Range -- ``range_options`` (``--min-<field>`` / ``--max-<field>``, inclusive).
4. Presence -- ``presence_option`` (``--has-<field>`` / ``--no-<field>`` tri-state).
5. Text-match -- plain ``@click.option`` (exact on ``list``; no decorator here).
6. Lifecycle -- ``include_disabled_option`` + ``time_window_options``.

``limit_option`` adds the sole result control (alongside filters, not a filter).

Help text in every decorator stays free of SPEC citations so the rendered
``--help`` surface carries no internal numbering.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

# Direction axis shared across email + workflow + template list commands.
DIRECTIONS = ["inbound", "outbound"]

_Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]


def limit_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add the canonical ``--limit`` result control (default 100)."""
    return click.option("--limit", default=100, help="Maximum results.")(fn)


def include_disabled_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add the ``--include-disabled`` lifecycle flag (default off)."""
    return click.option(
        "--include-disabled",
        is_flag=True,
        default=False,
        help="Include disabled rows (hidden by default).",
    )(fn)


def time_window_options(column: str) -> _Decorator:
    """Build ``--since`` / ``--until``: a closed inclusive window over ``column``.

    Args:
        column: The timestamp column the window filters, named in the help text.

    Returns:
        A decorator adding both ``--since`` and ``--until`` options.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn = click.option(
            "--until",
            default=None,
            help=f"ISO datetime inclusive upper bound on {column}.",
        )(fn)
        fn = click.option(
            "--since",
            default=None,
            help=f"ISO datetime inclusive lower bound on {column}.",
        )(fn)
        return fn

    return decorator


def scope_option(flag: str, param: str, help_text: str) -> _Decorator:
    """Build a parent-scope filter option.

    The referenced parent is resolved and validated in the command body
    (absent parent -> ``not_found`` envelope); this decorator only declares
    the option.

    Args:
        flag: The option flag (e.g. ``--company-id``).
        param: The destination parameter name.
        help_text: Help string (free of SPEC citations).

    Returns:
        A decorator adding the scope option.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return click.option(flag, param, default=None, help=help_text)(fn)

    return decorator


def enum_option(
    flag: str, param: str, choices: list[str], help_text: str
) -> _Decorator:
    """Build an enum-axis filter as a ``click.Choice`` over ``choices``.

    The choice set mirrors the schema CHECK constraint for the column, so an
    out-of-set value is rejected at parse time rather than silently ignored.

    Args:
        flag: The option flag (e.g. ``--status``).
        param: The destination parameter name.
        choices: The allowed values (mirrors the schema CHECK set).
        help_text: Help string (free of SPEC citations).

    Returns:
        A decorator adding the enum option.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return click.option(
            flag, param, default=None, type=click.Choice(choices), help=help_text
        )(fn)

    return decorator


def range_options(field: str, help_min: str, help_max: str) -> _Decorator:
    """Build ``--min-<field>`` / ``--max-<field>`` inclusive integer bounds.

    Both bounds are optional and compose into a closed range.

    Args:
        field: The field name (e.g. ``contacts``, ``email-confidence``); dashes
            in the flag become underscores in the parameter name.
        help_min: Help string for the lower bound.
        help_max: Help string for the upper bound.

    Returns:
        A decorator adding both range options.
    """
    param = field.replace("-", "_")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn = click.option(
            f"--max-{field}",
            f"max_{param}",
            type=int,
            default=None,
            help=help_max,
        )(fn)
        fn = click.option(
            f"--min-{field}",
            f"min_{param}",
            type=int,
            default=None,
            help=help_min,
        )(fn)
        return fn

    return decorator


def presence_option(field: str, help_text: str) -> _Decorator:
    """Build a ``--has-<field>`` / ``--no-<field>`` tri-state flag.

    Click derives a single ``has_<field>`` parameter from the positive switch
    (default ``None``), so there is no ``--no-has-<field>`` artifact and the
    three states are: ``True`` (``--has-<field>``), ``False`` (``--no-<field>``),
    ``None`` (neither given).

    Args:
        field: The field name (e.g. ``profile``); dashes become underscores in
            the parameter name.
        help_text: Help string (free of SPEC citations).

    Returns:
        A decorator adding the tri-state presence flag.
    """
    param = f"has_{field.replace('-', '_')}"

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return click.option(
            f"--has-{field}/--no-{field}",
            param,
            default=None,
            help=help_text,
        )(fn)

    return decorator
