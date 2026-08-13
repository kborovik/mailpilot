"""§I CLI API standard (§V.107, §V.115, §V.116) + §V.14: destructive-shape guardrail.

Prevents the §B118 regression where ``note remove`` keyed on an owner selector
alone bulk-deleted every note the owner held without an explicit confirm.
The §I CLI API standard fixes two rules this sweep enforces by walking the
live Click command tree:

  1. Closed verb vocabulary -- every leaf command name is a §I verb. A
     ``delete`` / ``purge`` / ``clear`` / ``wipe`` verb never reaches the tree
     (§I, "no `delete`").
  2. A ``remove`` is never satisfiable by owner selectors alone. Allowed
     shapes: positional id (``note remove <note_id>``, ``tag remove`` with
     required non-owner discriminator), or owner bulk gated by ``--yes``
     (§V.14 dual-mode). Owner selectors alone without confirm = §B118 class.
"""

from collections.abc import Iterator

import click

from mailpilot.cli import main

# §I entity verbs + the top-level / config verbs of §I.
ALLOWED_VERBS = {
    "list",
    "search",
    "view",
    "stats",
    "report",
    "create",
    "update",
    "disable",
    "enable",
    "add",
    "remove",
    "merge",
    "reply",
    "send",
    "start",
    "stop",
    "cancel",
    "retry",
    "run",
    "sync",
    "export",
    "import",
    "init",
    "migrate",
    "check",
    "get",
    "set",
    "status",
    "queue",  # leaf of `show queue` (§V.166 / §I Read)
}

# Owner selectors attach a sub-entity to a contact/company/workflow. Alone they
# match every row the owner holds -- the exact fan-out that broke note remove.
OWNER_SELECTORS = {"contact_email", "company_domain", "workflow_id"}


def _leaf_commands(
    command: click.Command, parent: click.Context | None = None
) -> Iterator[tuple[str, click.Command]]:
    """Yield (command_path, command) for every leaf (non-group) command."""
    info_name = command.name or "mailpilot"
    context = click.Context(command, info_name=info_name, parent=parent)
    if isinstance(command, click.Group):
        for subcommand in command.commands.values():
            yield from _leaf_commands(subcommand, context)
    else:
        yield context.command_path, command


def test_cli_verbs_are_closed_vocabulary() -> None:
    """§I: every leaf command name is a known verb. Bars a `delete` /
    `purge` / `clear` / `wipe` verb from ever entering the tree."""
    offenders = [
        f"`{path}` -> {command.name!r}"
        for path, command in _leaf_commands(main)
        if command.name not in ALLOWED_VERBS
    ]
    assert not offenders, (
        "§I CLI API standard bars verbs outside the closed vocabulary "
        "(notably any `delete`-class hard-delete); offenders: " + "; ".join(offenders)
    )


def test_remove_commands_not_owner_only() -> None:
    """§I / §V.14: a `remove` is never satisfiable by owner selectors alone.

    Allowed: positional id, required non-owner discriminator, or an explicit
    ``--yes`` confirm flag (owner bulk path). Owner selectors alone = §B118.
    """
    offenders: list[str] = []
    for path, command in _leaf_commands(main):
        if command.name != "remove":
            continue
        has_positional_id = any(
            isinstance(param, click.Argument) for param in command.params
        )
        has_required_discriminator = any(
            isinstance(param, click.Option)
            and param.required
            and param.name not in OWNER_SELECTORS
            for param in command.params
        )
        # Click is_flag options are never .required; name the confirm flag.
        has_yes_confirm = any(
            isinstance(param, click.Option) and param.name in {"yes", "confirmed"}
            for param in command.params
        )
        if not (has_positional_id or has_required_discriminator or has_yes_confirm):
            offenders.append(path)
    assert not offenders, (
        "§I / §V.14: a `remove` must name one row (positional id / required "
        "non-owner discriminator) or gate owner bulk with --yes; these are "
        "satisfiable by owner selectors alone: " + "; ".join(offenders)
    )
