# LLM Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Logfire LLM panels with token/cost tracking and simplify manual tool call counting.

**Architecture:** Call `logfire.instrument_pydantic_ai()` in `configure_logging()` for auto-instrumentation. Extract `result.usage()` onto `agent.invoke` and `agent.classify_email` spans. Replace `_count_successful_tool_calls()` with `usage.tool_calls`.

**Tech Stack:** pydantic-ai (`result.usage()`, `RunUsage`), logfire (`instrument_pydantic_ai`, `MetricsOptions`)

---

### Task 1: Instrument pydantic-ai in `configure_logging()`

**Files:**
- Modify: `src/mailpilot/cli.py:38-55`

- [ ] **Step 1: Add `MetricsOptions` and `instrument_pydantic_ai()` to `configure_logging()`**

In `src/mailpilot/cli.py`, update the `configure_logging` function:

```python
def configure_logging(debug: bool = False) -> None:
    """Configure Logfire from settings."""
    import logfire

    from mailpilot.settings import get_settings

    settings = get_settings()
    logfire.configure(
        service_name="mailpilot",
        environment=settings.logfire_environment,
        token=settings.logfire_token or None,
        console=logfire.ConsoleOptions(
            min_log_level="debug" if debug else "warn",
            show_project_link=False,
        ),
        send_to_logfire="if-token-present",
        inspect_arguments=False,
        metrics=logfire.MetricsOptions(collect_in_spans=True),
    )
    logfire.instrument_pydantic_ai()
```

- [ ] **Step 2: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: All checks pass.

- [ ] **Step 3: Commit**

```bash
git add src/mailpilot/cli.py
git commit -m "feat(cli): enable pydantic-ai auto-instrumentation with logfire"
```

---

### Task 2: Extract usage onto `agent.invoke` span and simplify tool counting

**Files:**
- Modify: `src/mailpilot/agent/invoke.py`
- Test: `tests/test_agent_invoke.py`

- [ ] **Step 1: Write failing test for usage attributes on `agent.invoke` span**

Add to `tests/test_agent_invoke.py`:

```python
def test_invoke_span_has_usage_attributes(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: Any,
) -> None:
    """agent.invoke span includes input_tokens, output_tokens, llm_requests."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    attrs = invoke_spans[0]["attributes"]
    assert "input_tokens" in attrs
    assert "output_tokens" in attrs
    assert "llm_requests" in attrs
    assert attrs["input_tokens"] >= 0
    assert attrs["output_tokens"] >= 0
    assert attrs["llm_requests"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_invoke.py::test_invoke_span_has_usage_attributes -xvs`
Expected: FAIL with `KeyError: 'input_tokens'` or `AssertionError`

- [ ] **Step 3: Implement usage extraction and simplify tool counting in `invoke.py`**

In `src/mailpilot/agent/invoke.py`:

1. Remove the `from pydantic_ai.messages import ToolReturnPart` import (line 25).

2. Delete the `_count_successful_tool_calls` function (lines 351-361) and its section comment (lines 348-349).

3. Replace lines 469-494 (after `result = agent.run_sync(...)`) with:

```python
            result = agent.run_sync(prompt, model=model, deps=deps)

            # Usage tracking.
            usage = result.usage()
            span.set_attribute("input_tokens", usage.input_tokens)
            span.set_attribute("output_tokens", usage.output_tokens)
            span.set_attribute("llm_requests", usage.requests)

            # Tool-use enforcement.
            tool_call_count = usage.tool_calls
            span.set_attribute("tool_call_count", tool_call_count)

            if tool_call_count == 0:
                logfire.warn(
                    "agent.no_tools_called",
                    workflow_id=workflow.id,
                    contact_id=contact.id,
                    agent_output=result.output,
                )
                raise AgentDidNotUseToolsError(
                    f"agent completed without calling any tools: "
                    f"workflow={workflow.id}, contact={contact.id}"
                )

            span.set_attribute("result", "completed")
            span.set_attribute("agent_reasoning", result.output)
            return {
                "workflow_id": workflow.id,
                "contact_id": contact.id,
                "status": "completed",
                "tool_calls": tool_call_count,
                "reasoning": result.output,
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_invoke.py -xvs`
Expected: All tests pass including `test_invoke_span_has_usage_attributes`.

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: All checks pass. `ToolReturnPart` import should be flagged as unused if not already removed.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/invoke.py tests/test_agent_invoke.py
git commit -m "feat(agent): extract LLM usage onto agent.invoke span, simplify tool counting"
```

---

### Task 3: Extract usage onto `agent.classify_email` span

**Files:**
- Modify: `src/mailpilot/agent/classify.py`
- Test: `tests/test_classify.py`

- [ ] **Step 1: Write failing test for usage attributes on classify span**

Add to `tests/test_classify.py`:

```python
def test_classify_span_has_usage_attributes(capfire: Any) -> None:
    """agent.classify_email span includes input_tokens, output_tokens."""
    workflow = make_workflow(
        "wf-sales-1",
        "Sales inbound",
        "Handle inbound pricing and demo requests",
    )
    run_classify(
        [workflow],
        function_model_returning(workflow_id="wf-sales-1", reasoning="pricing"),
    )

    classify_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.classify_email"
    ]
    assert len(classify_spans) == 1
    attrs = classify_spans[0]["attributes"]
    assert "input_tokens" in attrs
    assert "output_tokens" in attrs
    assert attrs["input_tokens"] >= 0
    assert attrs["output_tokens"] >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classify.py::test_classify_span_has_usage_attributes -xvs`
Expected: FAIL with `KeyError: 'input_tokens'` or `AssertionError`

- [ ] **Step 3: Add usage extraction to `classify_email` in `classify.py`**

In `src/mailpilot/agent/classify.py`, after `result = _AGENT.run_sync(prompt, model=model)` (line 110), add usage extraction before the existing `output = result.output` line:

```python
        result = _AGENT.run_sync(prompt, model=model)
        usage = result.usage()
        span.set_attribute("input_tokens", usage.input_tokens)
        span.set_attribute("output_tokens", usage.output_tokens)
        output = result.output
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classify.py -xvs`
Expected: All tests pass including `test_classify_span_has_usage_attributes`.

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: All checks pass.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/classify.py tests/test_classify.py
git commit -m "feat(agent): extract LLM usage onto classify_email span"
```

---

### Task 4: Full verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -x`
Expected: All tests pass (417+ tests), no regressions.

- [ ] **Step 2: Run full lint gate**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: All checks pass, 0 errors.

- [ ] **Step 3: Push to PR branch**

Run: `git push`
