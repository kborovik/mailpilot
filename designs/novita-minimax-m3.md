# feat(agent): swap to MiniMax-M3 via Novita Anthropic-compatible endpoint

## Problem

`anthropic_model` (default `claude-sonnet-4-6`, §I) → two PydanticAI model builders hard-wired to the Anthropic vendor endpoint:

- `agent/invoke.py:385 _build_anthropic_model` — workflow agent
- `agent/classify.py:57 _get_model` — classifier

Neither carries a base-URL knob → agent reaches only `api.anthropic.com`. Novita exposes an Anthropic-compatible endpoint @ `https://api.novita.ai/anthropic` w/ namespaced model ids, reachable via the stock Anthropic SDK by overriding `base_url`.

## Proposal

One new setting, threaded into the two existing `AnthropicProvider(...)` calls. No new model abstraction. No registry change (`AnthropicModel` takes arbitrary `model_name: str` → `minimax/minimax-m3` is a plain string). No `AsyncAnthropic` import (provider builds it internally).

1. **settings.py / §I** — add `anthropic_base_url: str | None` (default `None` → Anthropic's own endpoint); env override `MAILPILOT_ANTHROPIC_BASE_URL` per §V.85. Reuse `anthropic_api_key` (Novita key) + `anthropic_model` (Novita slug).

2. **invoke.py `_build_anthropic_model`** — one added arg:

```
provider=AnthropicProvider(
    api_key=settings.anthropic_api_key,
    base_url=settings.anthropic_base_url,        # None -> SDK default (Anthropic)
    http_client=httpx.AsyncClient(timeout=httpx.Timeout(240.0)),  # §V.48
),
```

3. **classify.py `_get_model`** — add `base_url` param + thread into lru_cache key so a base-URL change rebuilds the cached model:

```
@lru_cache(maxsize=4)
def _get_model(api_key: str, model_name: str, base_url: str | None) -> AnthropicModel:
    ...
    provider=AnthropicProvider(api_key=api_key, base_url=base_url,
                               http_client=httpx.AsyncClient(timeout=httpx.Timeout(240.0))),
```

Caller passes `settings.anthropic_base_url` alongside existing `anthropic_api_key` / `anthropic_model`.

4. **Switch = config-only:**

```
mailpilot config set anthropic_base_url https://api.novita.ai/anthropic
mailpilot config set anthropic_api_key   <novita-key>
mailpilot config set anthropic_model      minimax/minimax-m3
```

Unset `anthropic_base_url` → unchanged Anthropic behavior. Reversible, no redeploy.

## Naming

Keep `anthropic_*` setting names — they name the wire protocol (Anthropic Messages API), which is what's spoken; Novita is an Anthropic-protocol endpoint, not a different protocol. Vendor-neutral `llm_*` rename ripples through §I, §V.86 redaction, telemetry, tests → rejected as non-minimal.

## Effect on in-flight SPEC items

- **§I config** — add `anthropic_base_url` to settings key list (default `None`/unset).
- **§V.47** — unchanged in form. Cache flags stay on unconditionally (Decision: cache flags). Against Novita the `cache_read_input_tokens` / `cache_creation_input_tokens` span attrs may report 0 — observed via Logfire, not gated.
- **§V.48** — unchanged; 240s carried into the provider's `http_client`.
- **§V.86** — `anthropic_base_url` not secret; redacted-set unchanged.

## Design decisions

- **Decision (cache flags = option A):** leave `anthropic_cache_instructions` / `anthropic_cache_tool_definitions` on unconditionally, regardless of `anthropic_base_url`. **Why:** Anthropic-compat endpoints generally ignore unrecognized fields; keeping flags on holds §V.47 intact + avoids a provider-conditional branch. Monitor cache-token span attrs in Logfire to learn how Novita treats `cache_control`; revisit only on errors or pathological billing.
- **Decision (model id):** `minimax/minimax-m3` (Novita `namespace/model` format). **Why:** matches Novita catalog id format; wrong slug fails loudly @ call time, not in code.
- **Decision (provider path = one-liner, no shared helper):** thread `base_url` into both existing `AnthropicProvider(...)` calls rather than extract a shared builder or hand-build `AsyncAnthropic`. **Why:** installed `AnthropicProvider` accepts `api_key`+`base_url`+`http_client` together + builds `AsyncAnthropic(...)` internally (`anthropic.py:71-72,110`) — one-liner is the documented path + smallest diff; the two sites already co-maintain cache+timeout in sync, so a cross-module helper isn't warranted (YAGNI).

## Success criterion

- `anthropic_base_url` unset → model construction @ both sites behaves identically to today (Anthropic endpoint, 240s, cache flags).
- Set to `https://api.novita.ai/anthropic` + Novita key + `minimax/minimax-m3` → a live `agent.invoke` + a `classify_email` both reach Novita + return a valid completion / structured result.
- Default-None path covered by a test asserting `base_url` threads through both builders unchanged when unset.

## Out of scope

- Multi-provider routing / per-workflow model selection (YAGNI).
- Novita OpenAI-compatible endpoint (`/v3/openai`) — we use the Anthropic-compatible one to keep `AnthropicModel`.
- Extracting a shared `_build_anthropic_provider` helper — deferred; revisit only if a third call site appears.
