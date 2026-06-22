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


class AgentDidNotUseToolsError(MailPilotError):
    """Agent completed a run without calling any tools."""


class AgentCompletedWithoutReplyError(MailPilotError):
    """Inbound-trigger run ended without a reply or an explicit noop.

    Strengthens ``AgentDidNotUseToolsError`` (`§V.81`): a run may call
    search and read tools, satisfy the tool-count check, then narrate its
    intent to reply without ever calling ``reply_email`` / ``send_email``.
    For an inbound trigger that leaves the email unanswered, which is a
    failure -- not a silent ``completed`` (`§V.120`, `§B.103`).
    """
