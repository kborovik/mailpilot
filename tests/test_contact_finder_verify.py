"""§V.113 / §B.93 guard: contact-finder.md verifies emails via Bouncer's
real-time single GET endpoint, never the POST batch/sync endpoint.

§B.93: the agent verified via POST ``/email/verify/batch/sync``, which is
async-disguised-as-sync -- it returns an empty ``[]`` for cold/uncached emails
(verification runs in the background, scoring only on a later cached call). The
agent misread ``[]`` as Bouncer ``status=unknown`` and seeded every contact
with NULL ``email_confidence`` (the §B.76 all-NULL regression's upstream cause).
The fix swaps the verify step to the real-time single GET
``/v1.1/email/verify?email=``, ONE call per email (``<= 5``/company; per-email
billing so == batch credit cost) which returns the score inline on the first
call.

This mechanizes the §V.113 guard ("greps contact-finder.md = batch/sync absent
+ single-verify present"). "Absent" is scoped to the *invocation*: the
batch/sync endpoint must never appear as a live curl / endpoint URL. The
deliberate host-less "Do NOT use ... /email/verify/batch/sync" cautionary line
is the anti-regression note §B.93 earned and is explicitly allowed -- only an
active batch call regresses the bug.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_CONTACT_FINDER = _REPO_ROOT / ".claude" / "agents" / "contact-finder.md"

# The real-time single-verify GET endpoint (per-email) -- the §V.113 mandate.
_SINGLE_VERIFY_GET = '-G "https://api.usebouncer.com/v1.1/email/verify"'
# The async-disguised-as-sync batch endpoint, as a live curl / endpoint URL.
_BATCH_SYNC_URL = "https://api.usebouncer.com/v1.1/email/verify/batch/sync"


def _invokes_batch_sync(markdown: str) -> bool:
    """True if any line *invokes* the batch/sync endpoint (live curl / URL).

    A host-less ``/email/verify/batch/sync`` mention in a "Do NOT use"
    cautionary line is not an invocation -- it carries neither the full host
    URL nor a ``curl``/``-X POST`` verb, so it is excluded. Only a live call
    regresses §B.93.
    """
    if _BATCH_SYNC_URL in markdown:
        return True
    return any(
        "batch/sync" in line and ("curl" in line or "-X POST" in line)
        for line in markdown.splitlines()
    )


def test_contact_finder_uses_single_verify_get() -> None:
    """§V.113: the verify step calls the real-time single GET endpoint, one
    email per call (per-email ``email=`` query param)."""
    markdown = _CONTACT_FINDER.read_text()
    assert _SINGLE_VERIFY_GET in markdown, (
        "contact-finder.md must verify via the Bouncer real-time single GET "
        f"endpoint ({_SINGLE_VERIFY_GET})"
    )
    assert '--data-urlencode "email=' in markdown, (
        "single-verify GET must pass exactly one address via the `email=` "
        "query param (one call per email)"
    )


def test_contact_finder_does_not_invoke_batch_sync() -> None:
    """§V.113 / §B.93: the verify step never *invokes* the POST batch/sync
    endpoint (the empty-``[]``-for-cold-emails trap). A host-less cautionary
    mention is allowed; a live curl / URL is not."""
    markdown = _CONTACT_FINDER.read_text()
    assert not _invokes_batch_sync(markdown), (
        "contact-finder.md must not invoke Bouncer POST "
        "/email/verify/batch/sync -- it returns [] for cold emails and seeds "
        "all-NULL email_confidence (§B.93); use the real-time single GET "
        "endpoint instead"
    )


def test_batch_sync_detector_is_non_vacuous() -> None:
    """Guard against a tautological absence check -- restoring the batch/sync
    curl invocation must trip the detector, while the host-less cautionary
    mention alone must not (else the guard would pass over nothing)."""
    markdown = _CONTACT_FINDER.read_text()
    regressed = markdown + (
        "\n   curl -sS -X POST "
        '"https://api.usebouncer.com/v1.1/email/verify/batch/sync"\n'
    )
    assert _invokes_batch_sync(regressed), "detector misses a live batch/sync curl"
    assert not _invokes_batch_sync(
        "Do NOT use the `/email/verify/batch/sync` endpoint."
    ), "detector wrongly flags the allowed host-less cautionary mention"
