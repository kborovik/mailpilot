"""Shared Google credential helpers and delegated API client factory.

Authentication: service account credentials resolved in this order --

1. JSONB ``google_application_credentials`` on ``app_config``
   (``from_service_account_info`` + ``with_subject``)
2. Application Default Credentials (ADC) when that column is null --
   e.g. GCE metadata, Workload Identity, Cloud Run. DWD impersonation
   signs JWT assertions via the IAM Credentials API.

``GOOGLE_APPLICATION_CREDENTIALS`` is not a mailpilot settings source
(ADC may still consult it internally). No file-path setting.

Per-account impersonation via ``with_subject(email)`` for JSON creds,
or via ``service_account.Credentials(subject=email)`` over an
``iam.Signer`` for ADC-based credentials.

Required IAM in ADC mode: the active service account must hold
``roles/iam.serviceAccountTokenCreator`` on itself so it can sign JWTs
on its own behalf. The IAM Credentials API must be enabled.
"""

from __future__ import annotations

from typing import Any, ClassVar, Self

GOOGLE_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
"""HTTP statuses treated as transient on Google API ``HttpError``.

Shared with the agent retry classifier (`§V.49`, `§V.189`). 429 = quota /
rate-limit; 5xx = upstream blip. 529 is Anthropic-only (overloaded) and
must not appear here.
"""


def _google_sa_info(settings: Any | None = None) -> dict[str, Any] | None:
    """Return the JSONB service-account document, or None for ADC.

    ``GOOGLE_APPLICATION_CREDENTIALS`` is not read here; ADC may still
    consult it inside ``google.auth.default``.
    """
    if settings is None:
        from mailpilot.settings import get_settings

        settings = get_settings()
    info = settings.google_application_credentials
    if not info:
        return None
    return info


def has_google_credentials(settings: Any | None = None) -> bool:
    """True if any Google credential source is reachable.

    Checks the JSONB service-account document first, then probes ADC.
    Used by the sync loop to gate Pub/Sub subscriber startup and watch
    renewal so dev runs without GCP skip those branches.
    """
    if _google_sa_info(settings):
        return True
    from google.auth import default
    from google.auth.exceptions import DefaultCredentialsError

    try:
        default()
    except DefaultCredentialsError:
        return False
    return True


def _adc_service_account_email(source_credentials: Any) -> str:
    """Resolve the service account email backing ADC credentials.

    On a fresh ``compute_engine.Credentials`` instance the
    ``service_account_email`` attribute is the placeholder ``"default"``
    until the credentials are refreshed against the metadata server.
    """
    from google.auth.transport.requests import Request

    sa_email = getattr(source_credentials, "service_account_email", "") or ""
    if not sa_email or sa_email == "default":
        source_credentials.refresh(Request())
        sa_email = source_credentials.service_account_email
    return sa_email


def build_delegated_credentials(
    scopes: list[str], subject: str, settings: Any | None = None
) -> Any:
    """Build a service-account credential impersonating ``subject``.

    Uses the JSONB service-account document when present; otherwise falls
    back to ADC plus the IAM Credentials API for remote JWT signing. Both
    paths return a credential that performs domain-wide delegation for
    ``subject`` over ``scopes``.

    Args:
        scopes: OAuth scopes the returned credential is good for.
        subject: User email address to impersonate via DWD.
        settings: Optional settings snapshot; loaded if omitted.

    Returns:
        A google-auth credential ready for the googleapiclient
        ``build(..., credentials=...)`` call.
    """
    from google.oauth2.service_account import Credentials

    info = _google_sa_info(settings)
    if info:
        json_credentials = Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info,
            scopes=scopes,
        )
        return json_credentials.with_subject(subject)

    from google.auth import default, iam
    from google.auth.transport.requests import Request

    source_credentials, _ = default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    sa_email = _adc_service_account_email(source_credentials)
    signer = iam.Signer(Request(), source_credentials, sa_email)
    return Credentials(  # type: ignore[no-untyped-call]
        signer=signer,
        service_account_email=sa_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=scopes,
        subject=subject,
    )


def build_default_credentials(scopes: list[str], settings: Any | None = None) -> Any:
    """Build credentials for non-impersonated calls (e.g. Pub/Sub).

    Uses the JSONB service-account document when present; otherwise falls
    back to Application Default Credentials. Pinning to one source avoids
    the gcloud-user-login trap where an expired user token can send
    Pub/Sub into a 600-second gRPC retry loop.

    Args:
        scopes: OAuth scopes the returned credential is good for.
        settings: Optional settings snapshot; loaded if omitted.
    """
    info = _google_sa_info(settings)
    if info:
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info,
            scopes=scopes,
        )

    from google.auth import default

    credentials, _ = default(scopes=scopes)
    return credentials


def resolve_project_id(settings: Any | None = None) -> str:
    """Resolve the active GCP project ID.

    Reads ``project_id`` from the JSONB service-account document when
    present; otherwise asks ADC for the project bound to the active
    credentials.

    Raises:
        SystemExit: If neither source yields a project ID.
    """
    info = _google_sa_info(settings)
    if info:
        project_id = info.get("project_id")
        if not project_id:
            raise SystemExit("No project_id found in google_application_credentials")
        return project_id

    from google.auth import default

    _, project_id = default()
    if not project_id:
        raise SystemExit(
            "Could not resolve GCP project_id from Application Default "
            "Credentials -- set 'mailpilot config set "
            "google_application_credentials' to a service-account JSON "
            "object or run on an instance whose metadata server reports "
            "a project."
        )
    return project_id


def build_delegated_service(
    api: str,
    version: str,
    scopes: list[str],
    email: str,
    *,
    http: Any | None = None,
) -> Any:
    """Build a Google API service with domain-wide delegated credentials.

    Args:
        api: Discovery API name (``gmail``, ``drive``, ``calendar``).
        version: Discovery API version (``v1``, ``v3``).
        scopes: OAuth scopes the delegated credential is good for.
        email: User email to impersonate via domain-wide delegation.
        http: Optional ``httplib2.Http`` transport. When set, the service
            is built with an ``AuthorizedHttp`` wrapper instead of the
            default credentials argument (Drive uses this to cap socket
            timeout).

    Returns:
        googleapiclient discovery service resource.
    """
    from googleapiclient.discovery import build

    delegated = build_delegated_credentials(scopes, email)
    if http is not None:
        from google_auth_httplib2 import AuthorizedHttp

        authed_http = AuthorizedHttp(delegated, http=http)
        return build(api, version, http=authed_http)
    return build(api, version, credentials=delegated)


class GoogleClient:
    """Tiny per-account wrapper around a delegated Google API service.

    Subclasses set ``_api``, ``_version``, and ``_scopes``. Construction
    builds the delegated service via :func:`build_delegated_service`;
    tests should use :meth:`from_service`.
    """

    _api: ClassVar[str]
    _version: ClassVar[str]
    _scopes: ClassVar[list[str]]

    def __init__(self, email: str, *, http: Any | None = None) -> None:
        self.email = email
        self._service: Any = build_delegated_service(
            self._api, self._version, self._scopes, email, http=http
        )

    @classmethod
    def from_service(cls, email: str, service: Any) -> Self:
        """Create a client with a pre-built service (for testing).

        Args:
            email: Impersonated account email.
            service: Pre-built Google API service resource.

        Returns:
            Client using the provided service.
        """
        client = cls.__new__(cls)
        client.email = email
        client._service = service
        return client
