"""Domain exceptions for CLI error mapping.

All domain errors inherit from ``MailPilotError`` so the CLI can catch
them uniformly and call ``output_error()``. Add new exceptions only as
specific error paths are implemented -- do not pre-build a taxonomy.
"""


class MailPilotError(Exception):
    """Base exception for all MailPilot domain errors."""


class NotFoundError(MailPilotError):
    """Requested entity does not exist."""


class CooldownError(MailPilotError):
    """Outbound email blocked by cooldown period."""


class ClassificationError(MailPilotError):
    """Email classification failed."""


class SyncError(MailPilotError):
    """Gmail sync operation failed."""


class GmailBatchFetchError(SyncError):
    """Batch message fetch left messages unretrieved after retries.

    Raised by ``GmailClient.get_messages_batch`` when transient per-message
    failures (Gmail 429 "Too many concurrent requests" or 5xx) survive the
    bounded-backoff retry budget. Propagating rather than returning a partial
    list keeps ``sync_account`` from advancing ``gmail_history_id`` past mail
    that was never stored, so the next incremental sync re-collects it
    (`§V.75`, `§B.105`).
    """


class AgentDidNotUseToolsError(MailPilotError):
    """Agent completed a run without calling any tools."""


class AgentCompletedWithoutReplyError(MailPilotError):
    """Send-obligated run ended without a send or an explicit noop.

    Strengthens ``AgentDidNotUseToolsError`` (`§V.81`): a run may call
    search and read tools, satisfy the tool-count check, then narrate its
    intent to reply without ever calling ``reply_email`` / ``send_email``.
    Two trigger classes are send-obligated -- an inbound trigger that must
    answer (`§B.103`) and an outbound first reach-out that must open
    (`§B.106`). Leaving the message unsent is a failure, not a silent
    ``completed`` (`§V.120`).
    """
