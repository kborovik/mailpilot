# SPEC archive

Archived rows from `SPEC.md` per /sdd:compact prong 3 (window-vs-archive split). Sorted by id ascending. Verbatim preservation per archive-schema invariant. Eager-probed by `/sdd:check --all` cite-DAG sweep; excluded from `--incremental`.

## §T TASKS

id|status|task|cites
T1|x|spec ratified|-
T2|x|pair 3 unpaired logfire.exception sites in run.py / pubsub.py w/ operator_event|V51
T3|x|impl search_drive_markdown agent tool ∧ uplift smoke KB ≥10 docs|V34,V39,V64,V36
T4|x|harden smoke-test out-scope QA verifier — strip echoed-question tokens before fab regex|V56
T5|x|SUPERSEDED — §V.53 amend|V53
T6|x|SUPERSEDED — no duplicate routing|V22
T7|x|require pipe-table Markdown for spec sheets in demo workflow instr|-
T8|x|SUBSUMED by §T.15|V64,V41,V45
T9|x|impl _check_spec_table heuristic; reject spec-shape body w/o |---| separator|V39,V64,V42
T10|x|SUPERSEDED by §T.15|V44,V45
T11|x|impl §V.57 — qa.py source --id subcommand; B4 grading operator-judged|V57
T12|x|impl template registry (WorkflowTemplate, fragments, _CORE / _DRIVE)|V64,V44,V45,V46
T13|x|schema — add workflow.template NOT NULL + CHECK|V44
T14|x|database.create_workflow accepts template; rejects type mismatch ∧ template change|V44,V45
T15|x|refactor _build_agent — lookup template; drop _TOOLS ∧ _SYSTEM_PREFIX|V64,V44,V45,V46
T16|x|CLI — workflow create requires --template; new template noun (read-only)|V4,V5,V64,V44
T17|x|drop §V.43 path code (_workflow_uses_kb regex sniff)|V44,V45
T18|x|smoke-test wiring — Phase 0 swaps to --template; Scenario A regression for §B.10|V64,V44,V45
T19|x|sweep prompt fragments for single-tool examples — extend to ≥2 tools|V40
T20|x|filter trigger email from email_history in _build_user_prompt|V64,V29
T21|x|thread trigger arg through _build_user_prompt → _format_trigger|V26,V64,V30
T22|x|impl §V.47 — cache_control breakpoint + cache_*_input_tokens attrs on rollup|V64,V47
T23|x|impl /release skill — push, build wheel, gh release create|V62
T24|x|impl workflow export --account-id|V3,V4,V64,V63
T25|x|impl workflow import --account-id [--file]|V4,V64,V44,V63
T26|x|round-trip integration test for workflow export/import|V64,V63
T27|x|smoke-test Phase 0 swaps to workflow import w/ fixtures|V64,V63
T28|x|migrate company/contact export/import to §V.4 list envelope|V3,V4,V64,V63
T29|x|migrate tag remove ∧ enrollment remove to §V.4 singular envelope|V4,V64
T30|x|rewrite smoke-test Phase 5 per §V.58 — collapse to §1/§2/§3|V58
T31|x|widen _check_spec_table — drop numeric-value req; ASCII rule-lines ⊥ separator|V64,V42
T32|x|impl mailpilot --skill flag reading packaged SKILL.md|V4,V64,V6
T33|x|impl §V.48 — 240s httpx read-timeout on AnthropicProvider|V64,V47,V48
T34|x|impl §V.49 — bounded auto-retry for transient agent task failures|V51,V64,V47,V48,V49
T35|x|drop non-canonical run_loop ∧ its 3 tests|V21,V64,V65
T36|x|reshape mailpilot account sync output to §V.4 plural envelope|V4,V64
T37|x|delete orphan _sync_all_accounts in run.py|V65
T38|x|impl /demo-test skill per §V.59|V64,V52,V53,V57,V47,V58,V59
T39|x|fix smoke-test SKILL.md cite drift|-
T40|x|impl §V.60 + §V.59 G1 amend — Gmail-API poll, ⊥ local-DB|V37,V59,V60
T41|x|impl §V.54 — CRM CLI mutation telemetry matrix|V51,V64,V53,V63,V54
T42|x|patch dotless §-cites in src/ per §V.66|V66
T43|x|impl §V.31 — _DEFERRED_TASK trigger-aware|V64,V45,V30,V31
T44|x|impl §V.55 — logfire ScrubbingOptions; retain tool_response content|V64,V55
T45|x|patch dotless §-cites in tests/ per §V.66|V66
T46|x|impl §V.7 — add workflow_id/email_id/task_id to ActivitySummary|V64,V7
T47|x|strip company ∧ contact to identity-min; disabled_reason; --note STR atomic|V4,V5,V13,V14,V64,V63,V54,V7
T48|x|impl §V.8 — ContactView/CompanyView w/ inline notes|V4,V5,V13,V14,V39,V64,V45,V8

## §B BUGS

id|date|cause|fix
B1|2026-05-01|3 logfire.exception sites unpaired w/ operator_event ∴ stderr gaps under journald|V51
B2|2026-05-02|out-scope QA verifier flagged echoed-question digits as fabrications|V56
B3|2026-05-02|running tool span ⊥ structured tool_name (misdiagnosis — see §B.6)|V53
B4|2026-05-02|2 skipped_no_workflows spans — 2 distinct email_ids each routed once; §V.22 already held|V22
B5|2026-05-02|demo B1 reply rendered specs w/ asterisks ⊥ pipe-table — prompt-fidelity drift|V42
B6|2026-05-03|B3 misdiagnosis — gen_ai.tool.name already populated by instrument_pydantic_ai|V53
B7|2026-05-03|B4 grounded to broader doc, ⊥ model-specific — top-hit singular collapse, N=1 read|V41
B8|2026-05-03|B4 grounding gate substring-matched expected_tokens ∴ false-negative on phrasing variance|V57
B9|2026-05-03|B4 rendered specs as space-separated `<label>  <number>` ⊥ pipe-table — prompt-only fix didn't hold|V42
B10|2026-05-03|outbound A3 called search_drive_markdown ×5 — Drive tools unconditionally registered|V43
B11|2026-05-04|inbound trigger email inlined twice (Email history + New inbound email)|V29
B12|2026-05-04|A3 prompt rendered Deferred task block for operator-initiated enrollment_run|V30
B13|2026-05-08|company/contact export/import emitted {exported|imported: N, file} ⊥ §V.4 list envelope|V4
B14|2026-05-09|B4 KDF specs rendered w/ ASCII rule-line faux-separators — §V.42 regex required numerics|V42
B15|2026-05-10|GCE crash-loop 46+ iters — ⊥ google_application_credentials → ADC never tried|V37
B16|2026-05-10|2 prod TimeoutError at default 60s httpx read window on long-context agent.invoke|V48,V49
B22|2026-05-11|smoke-test skill carried dotless §V13/§V12/§T18/§B2 cites — auditor regex skipped|V66
B23|2026-05-11|/demo-test G1 polled local-DB; §V.59 setup forbids run-loop ∴ false-FAIL on healthy prod|V60
B24|2026-05-12|code-side §-cites carried dotless V13 / V11 — §V.66(+) binds project-wide|V66
B25|2026-05-12|outbound called record_enrollment_outcome on enrollment_run ∴ 2 terminal activity rows / enrollment|V31
B27|2026-05-12|mailpilot activity list omits workflow_id / email_id / task_id ∴ smoke-test falls back to psql|V7
B28|2026-05-12|outbound first-invoke cache_creation_input_tokens=0 — runtime path issue, ⊥ config|V50
B29|2026-05-14|mailpilot contact import w/o --file from TTY hangs on sys.stdin.read() — ⊥ isatty guard|V9
