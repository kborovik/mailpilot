# Add `mailpilot email reply` and unify CLI/agent send paths via `email_ops`

Date: 2026-04-26
Status: Approved (pre-implementation)

## Problem

The CLI and the internal Pydantic AI agent each have their own way to send mail, and they have drifted apart:

1. **Bypassed guards in the CLI.** `mailpilot email send` calls `sync.send_email` directly. The contact-status guard ("contact is bounced/unsubscribed") and the 30-day cold-outbound cooldown live only in `agent/tools.py:send_email`. Claude Code can therefore send to a bounced contact via CLI; the agent cannot. That is a backdoor, not a feature.
2. **Conflated send and reply.** The CLI uses `email send --thread-id ID` for replies. The operator must hand-build the recipient, re-type the subject (without missing the "Re:" prefix), and supply the thread id. The agent has a dedicated `reply_email` tool that derives all of those from the original email. The CLI is the harder, more error-prone surface despite being driven by an LLM operator.
3. **Duplicated business logic.** The agent's `send_email` and `reply_email` encode guards, contact resolution, subject prefixing, and enrollment activation. None of that is reachable from the CLI. Adding a CLI reply today would mean copy-pasting that logic.

The CLAUDE.md verb guidance was recently softened from "convention" to "guidance, not a rule" specifically to allow `email reply` as a domain verb when it reduces operator mistakes.

## Decision

1. Extract the policy layer that today lives in `agent/tools.py:send_email` / `reply_email` into a new module `mailpilot/email_ops.py`.
2. Both the agent tools and the CLI commands call into `email_ops`. The agent tools become thin wrappers that convert typed exceptions into LLM-facing error dicts; the CLI commands become thin wrappers that convert the same exceptions into `output_error` calls.
3. Add `mailpilot email reply` mirroring `email_ops.reply_email`.
4. Remove `--thread-id` and `--contact-id` from `mailpilot email send`. Replies go through `email reply`. Contact is auto-resolved from `--to`.
5. The CLI now applies the same guards as the agent. There is no admin override.

No compatibility shim. The CLI has no external users; Claude Code is the operator and updates with the codebase.

## Architecture

Three layers, top to bottom:

| Layer            | Module                | Responsibility                                                                  |
| ---------------- | --------------------- | ------------------------------------------------------------------------------- |
| Callers          | `cli.py`, `agent/tools.py` | Argument parsing, output formatting, exception-to-error-shape conversion.       |
| Policy (new)     | `email_ops.py`        | Guards (contact status, cooldown, reply preconditions), contact resolution, subject prefixing, enrollment activation. |
| Transport        | `sync.send_email`     | Build MIME, derive threading headers, call Gmail, persist `email` row.          |

`sync.send_email` is unchanged. `email_ops` is new. `agent/tools.py` and `cli.py` shrink.

## `email_ops` API

```python
def send_email(
    connection: psycopg.Connection,
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Send a new outbound email. Applies all guards.

    Auto-resolves contact_id from `to`. If the contact exists, applies:
      - contact-status guard (must be active)
      - cooldown guard (no unsolicited cold outbound within 30 days)
    If the contact does not exist, both guards are skipped (truly new
    contact). Activates the matching pending enrollment when both
    workflow_id and a resolved contact_id are present.

    Raises:
        ContactDisabled: contact is bounced/unsubscribed.
        Cooldown: prior unsolicited send within 30 days.
    """


def reply_email(
    connection: psycopg.Connection,
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    email_id: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Reply to an existing email in-thread.

    Derives recipient (contact.email), subject ("Re: " unless already
    prefixed), thread_id, and In-Reply-To from the original. Activates
    the matching pending enrollment when workflow_id is set. No
    cooldown -- replies are always allowed.

    Raises:
        OriginalNotFound: email_id does not exist.
        OriginalMissingThread: original has no gmail_thread_id.
        OriginalMissingContact: original has no contact_id.
        ContactMissing: original.contact_id does not resolve to a row.
        ContactDisabled: contact is bounced/unsubscribed.
    """
```

### Exceptions

Defined in `email_ops.py` as plain subclasses of a shared base:

```python
class EmailOpsError(Exception):
    """Base class. Carries `code` (str) and a human-readable message."""
    code: str

class ContactDisabled(EmailOpsError):
    code = "contact_disabled"

class Cooldown(EmailOpsError):
    code = "cooldown"

class OriginalNotFound(EmailOpsError):
    code = "not_found"

class OriginalMissingThread(EmailOpsError):
    code = "no_thread"

class OriginalMissingContact(EmailOpsError):
    code = "no_contact"

class ContactMissing(EmailOpsError):
    code = "not_found"
```

The `code` strings exactly match the strings the agent tools return today, so the agent prompt and any LLM expectations stay correct.

## Caller wrappers

### Agent tools (`agent/tools.py`)

Both `send_email` and `reply_email` shrink to:

```python
def send_email(connection, account, gmail_client, settings, workflow_id, to, subject, body, cc=None, bcc=None):
    try:
        email = email_ops.send_email(
            connection, account, gmail_client, settings,
            to=to, subject=subject, body=body,
            workflow_id=workflow_id, cc=cc, bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}
    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }
```

`workflow_id` stays required at the agent boundary because every agent invocation runs inside a workflow. The agent passes it through to `email_ops`, which treats it as optional internally.

### CLI commands (`cli.py`)

```
mailpilot email send  --account-id ID --to E [--to E2 ...] --subject S --body B
                      [--workflow-id ID] [--cc E] [--bcc E]

mailpilot email reply --account-id ID --email-id ID --body B
                      [--workflow-id ID] [--cc E] [--bcc E]
```

Both commands:

1. Validate empty inputs (`--body`, `--subject`) before opening the DB.
2. Open the DB; resolve `account` via `get_account`; emit `not_found` if missing.
3. Optional FK validation: if `--workflow-id` is supplied, `get_workflow` it and emit `not_found` if missing.
4. Build the Gmail client and call `email_ops.send_email` / `email_ops.reply_email`.
5. On `EmailOpsError`, call `output_error(str(exc), exc.code)`.
6. On success, `output(email.model_dump(mode="json"))`.

`email send` no longer accepts `--contact-id` or `--thread-id`. Multi-recipient `--to` keeps the existing comma-join behaviour.

## Behaviour changes for the CLI

- `email send` now refuses to send to bounced/unsubscribed contacts. Exits with `contact_disabled`.
- `email send` now enforces the 30-day cold-outbound cooldown. Exits with `cooldown`.
- `email send --thread-id` removed. Use `email reply` instead.
- `email send --contact-id` removed. Contact is auto-resolved from `--to`.
- `email reply` is new.

## CLAUDE.md updates

The CLI command listing in CLAUDE.md changes:

```
mailpilot email send  --account-id ID --to E [--to E2 ...] --subject S --body B [--workflow-id ID] [--cc E] [--bcc E]
mailpilot email reply --account-id ID --email-id ID --body B [--workflow-id ID] [--cc E] [--bcc E]
```

(Old line:
`mailpilot email send --account-id ID --to E [--to E2 ...] --subject S --body B [--contact-id ID] [--workflow-id ID] [--thread-id ID] [--cc E] [--bcc E]`
is removed.)

## Testing

Following the project's TDD process. Tests run against `mailpilot_test` and use `pytest-httpx` for Gmail mocking; new tests must patch at the source module (`mailpilot.email_ops.func`, not `mailpilot.cli.func`).

### `email_ops` unit tests (new file `tests/test_email_ops.py`)

`send_email`:
- happy path with no contact (new email address) -- returns Email, no guard fires.
- happy path with active contact -- returns Email.
- contact bounced -> `ContactDisabled`.
- contact unsubscribed -> `ContactDisabled`.
- cooldown active -> `Cooldown`.
- workflow_id provided + pending enrollment -> enrollment becomes `active`.
- workflow_id provided + no enrollment row -> no error, no side effect.

`reply_email`:
- happy path -> returns Email; subject gains "Re: " when missing.
- subject already "Re: ..." -> not double-prefixed.
- email_id missing -> `OriginalNotFound`.
- original has no thread_id -> `OriginalMissingThread`.
- original has no contact_id -> `OriginalMissingContact`.
- contact deleted between original and reply -> `ContactMissing`.
- contact bounced -> `ContactDisabled`.
- workflow_id provided + pending enrollment -> activated.

### Agent-tool tests

The existing tests in `tests/test_agent_tools.py` (or equivalent) for `send_email` / `reply_email` keep their public contract (return shape) but their internals change. Tests assert the dict shape after each `email_ops` exception path: every `EmailOpsError` subclass produces `{"error": code, "message": str}`.

### CLI tests

In `tests/test_cli.py`:

- `email send`: drop tests that pass `--contact-id` / `--thread-id`. Add tests that patch `email_ops.send_email` and assert (a) success returns a JSON Email row, (b) each `EmailOpsError` yields `output_error` with the matching code.
- `email reply`: new test cases mirroring the `email_ops.reply_email` exception matrix, plus account-not-found and workflow-not-found.

### E2E

The `/smoke-test` flow already covers outbound send + inbound reply via the agent. After this change it continues to pass without modification (agent return shapes are identical). No new e2e tests required.

## Migration

Single PR. No compatibility shim. Order of changes within the PR:

1. Create `email_ops.py` with the two functions, exception hierarchy, and tests.
2. Refactor `agent/tools.py` `send_email` / `reply_email` to call `email_ops`. Existing agent tests keep passing on the dict contract.
3. Refactor `cli.py` `email send` to drop `--contact-id` / `--thread-id` and call `email_ops.send_email`. Update `email send` tests.
4. Add `cli.py` `email reply` calling `email_ops.reply_email`. Add tests.
5. Update CLAUDE.md command listing.

## Out of scope

- Reply-all (preserving cc/from from the original). The agent does not do this today; deferred until an operator request demonstrates need.
- Subject override on reply. If the operator wants a different subject, the message is a new conversation -- they should use `email send`.
- Auto-inheriting cc/bcc from the original. Same reasoning as reply-all; deferred.
- An admin override flag to bypass cooldown / contact-status from the CLI. Reactivating the contact (or extending the cooldown logic) is the explicit, auditable path.
