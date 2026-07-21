"""Provider-aware LLM model construction for classifier and workflow agents.

§V.47: ``llm_provider`` dispatches to Anthropic or xAI. Active-provider API key
is required at build; inactive-provider keys may be empty. Workflow-role model
settings (thinking/effort/max_tokens) never apply to the classifier.
"""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.xai import XaiModel, XaiModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.xai import XaiProvider

from mailpilot.settings import Settings

ModelRole = Literal["classifier", "workflow"]

# §V.48: hard-coded transport timeout for both providers (no operator setting).
_PROVIDER_TIMEOUT_SECONDS = 240.0


def active_model_name(settings: Settings) -> str:
    """Return the configured model id for the active ``llm_provider``."""
    if settings.llm_provider == "anthropic":
        return settings.anthropic_model
    return settings.xai_model


def _build_model(  # pyright: ignore[reportUnusedFunction]
    settings: Settings, *, role: ModelRole
) -> Model:
    """Build the active-provider model for ``role``.

    Args:
        settings: Application settings (provider switch + per-provider knobs).
        role: ``classifier`` excludes workflow-only reasoning/max_tokens knobs;
            ``workflow`` includes them.

    Returns:
        A pydantic-ai ``Model`` bound to the active provider.

    Raises:
        ValueError: If the active provider's API key is empty.
    """
    if settings.llm_provider == "anthropic":
        return _build_anthropic_model(settings, role=role)
    return _build_xai_model(settings, role=role)


def _build_anthropic_model(settings: Settings, *, role: ModelRole) -> AnthropicModel:
    """Construct an AnthropicModel with §V.47 cache flags and §V.48 timeout.

    Cache breakpoints on the system prompt and tool definitions let multi-turn
    invocations re-bill the stable prefix as ``cache_read_input_tokens``.

    The HTTP client carries a 240s read-timeout so long-context calls do not
    surface ``TimeoutError`` mid-conversation (retry is unsafe after tool
    side-effects; §V.48).

    ``anthropic_base_url`` is the wire endpoint. It defaults to
    ``api.anthropic.com``; an Anthropic-compatible override (e.g. Novita)
    re-targets the Messages API with no code change.

    Workflow role only: ``anthropic_thinking`` / ``anthropic_effort`` are
    empty-gated; ``anthropic_max_tokens`` is always passed as ``max_tokens``
    so default-active thinking cannot exhaust the provider-default budget
    before reply text (§B.115). The classifier never receives these knobs.
    """
    if not settings.anthropic_api_key:
        raise ValueError(
            "anthropic_api_key is required when llm_provider=anthropic; "
            "set it via `mailpilot config set anthropic_api_key ...`",
        )
    model_settings = AnthropicModelSettings(
        anthropic_cache_tool_definitions=True,
        anthropic_cache_instructions=True,
    )
    if role == "workflow":
        model_settings["max_tokens"] = settings.anthropic_max_tokens
        if settings.anthropic_thinking:
            model_settings["anthropic_thinking"] = {"type": settings.anthropic_thinking}
        if settings.anthropic_effort:
            model_settings["anthropic_effort"] = settings.anthropic_effort
    return AnthropicModel(
        settings.anthropic_model,
        provider=AnthropicProvider(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(_PROVIDER_TIMEOUT_SECONDS)
            ),
        ),
        settings=model_settings,
    )


def _build_xai_model(settings: Settings, *, role: ModelRole) -> XaiModel:
    """Construct an XaiModel with §V.48 timeout and workflow-only effort/budget.

    No Anthropic cache flags (omit -- no false cache telemetry). ``api_host`` is
    optional for gateway/proxy; empty string means SDK default host.

    Workflow role: ``xai_reasoning_effort`` + ``xai_max_tokens`` always passed
    (effort has no empty/none; Grok 4.5 always reasons). Classifier role omits
    both.
    """
    if not settings.xai_api_key:
        raise ValueError(
            "xai_api_key is required when llm_provider=xai; "
            "set it via `mailpilot config set xai_api_key ...`",
        )
    provider_kwargs: dict[str, object] = {
        "api_key": settings.xai_api_key,
        "timeout": _PROVIDER_TIMEOUT_SECONDS,
    }
    if settings.xai_api_host:
        provider_kwargs["api_host"] = settings.xai_api_host
    model_settings: XaiModelSettings | None = None
    if role == "workflow":
        model_settings = XaiModelSettings(
            max_tokens=settings.xai_max_tokens,
            xai_reasoning_effort=settings.xai_reasoning_effort,
        )
    return XaiModel(
        settings.xai_model,
        provider=XaiProvider(**provider_kwargs),  # type: ignore[arg-type]
        settings=model_settings,
    )
