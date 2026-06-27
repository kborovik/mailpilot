"""§I CLI API standard (§V.107, §V.115, §V.116) + §V.14: destructive-shape guardrail.

Prevents the §B118 / §T199 regression where ``note remove`` keyed on an owner
selector (``--contact-email`` / ``--company-domain``) and bulk-deleted every note
the owner held. The §I CLI API standard fixes two rules this sweep enforces by
walking the live Click command tree, so a reintroduced bulk-delete fails CI
before it ships:

  1. Closed verb vocabulary -- every leaf command name is a §I verb. A
     ``delete`` / ``purge`` / ``clear`` / ``wipe`` verb never reaches the tree
     (§I line 25, "no `delete`").
  2. A ``remove`` command names exactly one row -- via a positional id argument
     (``note remove <note_id>``, the sole hard-delete §V.14) or a required
     non-owner discriminator (``tag remove --tag`` identifies one link). Owner
     selectors alone never satisfy a ``remove``, so no destructive verb can fan
     out across an owner's rows again.
"""

from collections.abc import Iterator

import click

from mailpilot.cli import main

# §I line 25 entity verbs + the top-level / config verbs of §I line 26.
ALLOWED_VERBS = {
    "list",
    "search",
    "view",
    "stats",
    "create",
    "update",
    "disable",
    "enable",
    "add",
    "remove",
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
    """§I line 25: every leaf command name is a known verb. Bars a `delete` /
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


def test_remove_commands_name_a_single_row() -> None:
    """§I / §V.14: a `remove` command names exactly one row -- a positional id
    or a required non-owner discriminator. A `remove` satisfiable by owner
    selectors alone would bulk-delete every row the owner holds (§B118)."""
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
        if not has_positional_id and not has_required_discriminator:
            offenders.append(path)
    assert not offenders, (
        "§I / §V.14: a `remove` must name one row via a positional id or a "
        "required non-owner discriminator; these are satisfiable by owner "
        "selectors alone and could bulk-delete: " + "; ".join(offenders)
    )
