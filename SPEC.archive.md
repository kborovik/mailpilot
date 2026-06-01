# SPEC archive

Archived rows from `SPEC.md` per /sdd:compact prong 3 (window-vs-archive split). Sorted by id ascending. Verbatim preservation per archive-schema invariant. Eager-probed by `/sdd:check --all` cite-DAG sweep; excluded from `--incremental`.

## §T TASKS

id|status|task|cites
T1|x|spec ratified|-
T2|x|pair 3 unpaired logfire.exception sites in run.py / pubsub.py w/ operator_event|V19
T3|x|impl search_drive_markdown agent tool ∧ uplift smoke KB ≥10 docs|V14,V15,V20,V24
T4|x|harden smoke-test out-scope QA verifier — strip echoed-question tokens before fab regex|V25
T5|x|SUPERSEDED — §V.26 amend|V26
T6|x|SUPERSEDED — no duplicate routing|V27
T7|x|require pipe-table Markdown for spec sheets in demo workflow instr|-
T8|x|SUBSUMED by §T.15|V20,V28,V33
T9|x|impl _check_spec_table heuristic; reject spec-shape body w/o |---| separator|V15,V20,V29
T10|x|SUPERSEDED by §T.15|V32,V33
T11|x|impl §V.31 — qa.py source --id subcommand; B4 grading operator-judged|V31
T12|x|impl template registry (WorkflowTemplate, fragments, _CORE / _DRIVE)|V20,V32,V33,V34
T13|x|schema — add workflow.template NOT NULL + CHECK|V32
T14|x|database.create_workflow accepts template; rejects type mismatch ∧ template change|V32,V33
T15|x|refactor _build_agent — lookup template; drop _TOOLS ∧ _SYSTEM_PREFIX|V20,V32,V33,V34
T16|x|CLI — workflow create requires --template; new template noun (read-only)|V5,V6,V20,V32
T17|x|drop §V.30 path code (_workflow_uses_kb regex sniff)|V32,V33
T18|x|smoke-test wiring — Phase 0 swaps to --template; Scenario A regression for §B.10|V20,V32,V33
T19|x|sweep prompt fragments for single-tool examples — extend to ≥2 tools|V16
T20|x|filter trigger email from email_history in _build_user_prompt|V20,V35
T21|x|thread trigger arg through _build_user_prompt → _format_trigger|V11,V20,V36
T22|x|impl §V.37 — cache_control breakpoint + cache_*_input_tokens attrs on rollup|V20,V37
T23|x|impl /release skill — push, build wheel, gh release create|V38
T24|x|impl workflow export --account-id|V4,V5,V20,V39
T25|x|impl workflow import --account-id [--file]|V5,V20,V32,V39
T26|x|round-trip integration test for workflow export/import|V20,V39
T27|x|smoke-test Phase 0 swaps to workflow import w/ fixtures|V20,V39
T28|x|migrate company/contact export/import to §V.5 list envelope|V4,V5,V20,V39
T29|x|migrate tag remove ∧ enrollment remove to §V.5 singular envelope|V5,V20
T30|x|rewrite smoke-test Phase 5 per §V.40 — collapse to §1/§2/§3|V40
T31|x|widen _check_spec_table — drop numeric-value req; ASCII rule-lines ⊥ separator|V20,V29
T32|x|impl mailpilot --skill flag reading packaged SKILL.md|V5,V20,V41
T33|x|impl §V.43 — 240s httpx read-timeout on AnthropicProvider|V20,V37,V43
T34|x|impl §V.44 — bounded auto-retry for transient agent task failures|V19,V20,V37,V43,V44
T35|x|drop non-canonical run_loop ∧ its 3 tests|V3,V20,V21
T36|x|reshape mailpilot account sync output to §V.5 plural envelope|V5,V20
T37|x|delete orphan _sync_all_accounts in run.py|V21
T38|x|impl /demo-test skill per §V.45|V20,V22,V26,V31,V37,V40,V45
T39|x|fix smoke-test SKILL.md cite drift|-
T40|x|impl §V.46 + §V.45 G1 amend — Gmail-API poll, ⊥ local-DB|V42,V45,V46
T41|x|impl §V.47 — CRM CLI mutation telemetry matrix|V19,V20,V26,V39,V47
T42|x|patch dotless §-cites in src/ per §V.48|V48
T43|x|impl §V.49 — _DEFERRED_TASK trigger-aware|V20,V33,V36,V49
T44|x|impl §V.50 — logfire ScrubbingOptions; retain tool_response content|V20,V50
T45|x|patch dotless §-cites in tests/ per §V.48|V48
T46|x|impl §V.51 — add workflow_id/email_id/task_id to ActivitySummary|V20,V51
T47|x|strip company ∧ contact to identity-min; disabled_reason; --note STR atomic|V5,V6,V8,V9,V20,V39,V47,V51
T48|x|impl §V.53 — ContactView/CompanyView w/ inline notes|V5,V6,V8,V9,V15,V20,V33,V53

## §B BUGS

id|date|cause|fix
B1|2026-05-01|3 logfire.exception sites unpaired w/ operator_event ∴ stderr gaps under journald|V19
B2|2026-05-02|out-scope QA verifier flagged echoed-question digits as fabrications|V25
B3|2026-05-02|running tool span ⊥ structured tool_name (misdiagnosis — see §B.6)|V26
B4|2026-05-02|2 skipped_no_workflows spans — 2 distinct email_ids each routed once; §V.27 already held|V27
B5|2026-05-02|demo B1 reply rendered specs w/ asterisks ⊥ pipe-table — prompt-fidelity drift|V29
B6|2026-05-03|B3 misdiagnosis — gen_ai.tool.name already populated by instrument_pydantic_ai|V26
B7|2026-05-03|B4 grounded to broader doc, ⊥ model-specific — top-hit singular collapse, N=1 read|V28
B8|2026-05-03|B4 grounding gate substring-matched expected_tokens ∴ false-negative on phrasing variance|V31
B9|2026-05-03|B4 rendered specs as space-separated `<label>  <number>` ⊥ pipe-table — prompt-only fix didn't hold|V29
B10|2026-05-03|outbound A3 called search_drive_markdown ×5 — Drive tools unconditionally registered|V30
B11|2026-05-04|inbound trigger email inlined twice (Email history + New inbound email)|V35
B12|2026-05-04|A3 prompt rendered Deferred task block for operator-initiated enrollment_run|V36
B13|2026-05-08|company/contact export/import emitted {exported|imported: N, file} ⊥ §V.5 list envelope|V5
B14|2026-05-09|B4 KDF specs rendered w/ ASCII rule-line faux-separators — §V.29 regex required numerics|V29
B15|2026-05-10|GCE crash-loop 46+ iters — ⊥ google_application_credentials → ADC never tried|V42
B16|2026-05-10|2 prod TimeoutError at default 60s httpx read window on long-context agent.invoke|V43,V44
B22|2026-05-11|smoke-test skill carried dotless §V13/§V12/§T18/§B2 cites — auditor regex skipped|V48
B23|2026-05-11|/demo-test G1 polled local-DB; §V.45 setup forbids run-loop ∴ false-FAIL on healthy prod|V46
B24|2026-05-12|code-side §-cites carried dotless V13 / V11 — §V.48(+) binds project-wide|V48
B25|2026-05-12|outbound called record_enrollment_outcome on enrollment_run ∴ 2 terminal activity rows / enrollment|V49
B27|2026-05-12|mailpilot activity list omits workflow_id / email_id / task_id ∴ smoke-test falls back to psql|V51
B28|2026-05-12|outbound first-invoke cache_creation_input_tokens=0 — runtime path issue, ⊥ config|V52
B29|2026-05-14|mailpilot contact import w/o --file from TTY hangs on sys.stdin.read() — ⊥ isatty guard|V54
