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
