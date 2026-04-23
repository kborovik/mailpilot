# LLM Observability: Cost and Tool Calling Metrics

**Date:** 2026-04-23
**Scope:** Add Logfire LLM panels, token/cost tracking, and simplify tool call counting

## Problem

The agent layer has no visibility into LLM token usage, cost, or detailed tool call sequences. The manual `_count_successful_tool_calls()` function in `invoke.py` duplicates counting that pydantic-ai already provides via `result.usage()`.

## Design

### 1. Instrumentation registration -- `cli.py:configure_logging()`

Add `logfire.instrument_pydantic_ai()` after `logfire.configure()`. This auto-instruments all `Agent.run_sync()` calls, generating OpenTelemetry gen_ai spans with:

- Token counts (input/output)
- Cost (`operation.cost`)
- Tool call details (name, args, return value)
- Message sequence (system/user/assistant/tool)
- LLM conversation panels in Logfire UI

Add `metrics=logfire.MetricsOptions(collect_in_spans=True)` to `logfire.configure()` so token/cost metrics aggregate onto parent spans.

### 2. Usage attributes on `agent.invoke` span -- `invoke.py`

After `agent.run_sync()`, extract `result.usage()` and set span attributes:

- `input_tokens` -- input token count
- `output_tokens` -- output token count
- `llm_requests` -- number of LLM round-trips

No cost attribute -- `instrument_pydantic_ai()` records `operation.cost` on gen_ai child spans, and `collect_in_spans=True` aggregates it onto the parent.

### 3. Simplify tool call counting -- `invoke.py`

Replace `_count_successful_tool_calls(result.all_messages())` with `result.usage().tool_calls`. Both count successful tool executions only (pydantic-ai docs: "The execution counter only increments upon the successful invocation of a tool").

Remove:

- `_count_successful_tool_calls()` function
- `from pydantic_ai.messages import ToolReturnPart` import

Keep unchanged:

- `tool_call_count` span attribute (sourced from `usage.tool_calls`)
- `AgentDidNotUseToolsError` enforcement when count is 0

### 4. Usage attributes on `classify_email` span -- `classify.py`

Extract `result.usage()` onto the `agent.classify_email` span:

- `input_tokens` -- input token count
- `output_tokens` -- output token count

No `tool_calls` attribute -- classifier is single-turn with no tools.

### 5. Testing

- Span contract test: verify `agent.invoke` span has `input_tokens`, `output_tokens`, `llm_requests` attributes (using `capfire` fixture + `FunctionModel`)
- Tool call count equivalence: verify `usage.tool_calls` matches expected count in existing `test_agent_invoke.py` tests
- Classify usage test: verify `agent.classify_email` span has token attributes
- No dedicated test for `instrument_pydantic_ai()` -- gen_ai child spans are Logfire's responsibility

## Files changed

| File | Change |
|------|--------|
| `src/mailpilot/cli.py` | Add `MetricsOptions` to `logfire.configure()`, add `logfire.instrument_pydantic_ai()` |
| `src/mailpilot/agent/invoke.py` | Extract `result.usage()` onto span, replace `_count_successful_tool_calls` with `usage.tool_calls`, remove `ToolReturnPart` import |
| `src/mailpilot/agent/classify.py` | Extract `result.usage()` onto span |
| `tests/test_agent_invoke.py` | Add span contract test for usage attributes |
| `tests/test_observability.py` | Add classify usage span test |

## Out of scope

- Custom Logfire dashboards or alerts (can be configured in Logfire UI later)
- Token budgets or rate limiting (future work if needed)
- Cost calculation in CLI output (Logfire UI already shows this)
