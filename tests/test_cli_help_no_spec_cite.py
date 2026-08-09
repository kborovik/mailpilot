"""§V.111: operator-facing CLI ``--help`` carries zero SPEC citation.

Operator-facing twin of §V.45 (agent-prompt text). Click derives a command's
rendered help from its function docstring (long + truncated short help) and from
each option's ``help=`` string; a literal ``§V/§T/§B.<n>`` token there is dead
authoring metadata leaking to a CLI operator who has no SPEC.md (closes §B.91).

Top-level ``mailpilot --help`` is the packaged SKILL.md body (not Click
help); this sweep walks subcommand/verb ``--help`` only (not root). SKILL.md
itself is checked separately for zero §-cites.
"""

import re
from collections.abc import Iterator
from importlib.resources import files

import click

from mailpilot.cli import main

_SPEC_CITE = re.compile(r"§[VTB]\.[0-9]+")


def _walk_commands(
    command: click.Command, parent: click.Context | None = None
) -> Iterator[tuple[str, click.Command, click.Context]]:
    """Yield (command_path, command, context) for the command and its subtree."""
    info_name = command.name or "mailpilot"
    context = click.Context(command, info_name=info_name, parent=parent)
    yield context.command_path, command, context
    if isinstance(command, click.Group):
        for subcommand in command.commands.values():
            yield from _walk_commands(subcommand, context)


def test_cli_help_carries_no_spec_citation() -> None:
    """§V.111 / §B.91: every subcommand/verb Click ``--help`` renders free of a
    ``§V/§T/§B.<n>`` token -- docstring-derived help and option ``help=`` text
    alike. Root is excluded: top-level ``--help`` emits SKILL.md instead."""
    offenders: list[str] = []
    for command_path, command, context in _walk_commands(main):
        # Root help is SKILL.md (tested elsewhere); only Click-rendered help.
        if command is main:
            continue
        help_text = command.get_help(context)
        match = _SPEC_CITE.search(help_text)
        if match is not None:
            offenders.append(f"`{command_path} --help` -> {match.group()!r}")
    assert not offenders, (
        "§V.111 forbids §-numbering in operator-facing CLI --help "
        "(docstring-derived help + option help= text); offenders: "
        + "; ".join(offenders)
    )


def test_skill_md_carries_no_spec_citation() -> None:
    """§V.111: packaged SKILL.md body has zero §V/§T/§B.<n> tokens."""
    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    match = _SPEC_CITE.search(body)
    assert match is None, f"SKILL.md carries SPEC cite {match.group()!r}"
