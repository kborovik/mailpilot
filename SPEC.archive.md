# SPEC.archive.md

## §T TASKS

id|status|task|cites
T109|x|impl §V.51(+) per §B.71 — pair connect-fail logfire.exception w/ operator_event("error") + sweep contract test for all run-reachable logfire.exception sites|V51,B71
T110|x|add contact.title + contact.email_confidence columns w/ CHECK; Contact model + CLI --title/--email-confidence flags; contact list --max-email-confidence filter; TDD|V95
T111|x|rename lead-encreach -> lead-companies skill dir + workflow file + frontmatter; update all intra-skill refs; cite-DAG sweep|V73,B72
T112|x|new lead-contacts skill + lead-contacts-find.js + contact-finder agent — Hunter -> TheOrg -> Bouncer pipeline, seed <= 5 contacts/company; workflow byte-identical to skill-mirror per §V.73|V96,V73
T113|x|impl §V.3(+) per §B.73 — ConsoleOptions(output=sys.stderr) in configure_logging + recurrence-guard test (schema-drift path)|V3,B73
T114|x|impl §V.97(+) per §B.74 — add deferred field to lead-companies + lead-contacts Run summary when batch gate caps; skill-prose only|V97,B74
T115|x|impl §V.98(+) per §B.75 — seed_companies.py: name-divergent duplicate_key -> collapsed (not bare existing); Run summary prose mirrors|V98,B75
T116|x|impl §V.5(+): ContactSummary + search_contacts add title + company_domain (LEFT JOIN); --min/max-email-confidence, --company-domain, --title filters; TDD|V5,V95
T117|x|impl §V.95(+) per §B.76 — list_contacts OR IS NULL disjunct for NULL email_confidence + docstring fix + recurrence-guard test|V95,B76
T118|x|impl §V.99(+) per §B.77 — smoke-test SKILL.md dead KB-sync refs → manual Drive-upload path|V99,B77
T119|x|impl §V.100(+) — smoke-test SKILL.md 1254-line split; Phase 5 report + derivation aids → `references/*.md`|V100
T120|x|add §V.73 drift-guard — mechanical test asserting skill-body workflow snippets byte-identical to saved `.claude/workflows/*.js`|V73
T121|x|impl §V.101(+) — replace must-sense ` ! ` w/ `MUST` in `.claude/skills/**`|V101
T122|x|fix stale scenario count in smoke-test SKILL.md: "Two scenarios" -> "Three"|-
T123|x|impl §V.100(+) sibling-DRY — shared lead-companies/lead-contacts prose → `references/lead-pipeline-conventions.md`|V100
T124|x|add demo-test Prerequisites note coupling smoke-test/scripts/qa.py dependency|V99
T125|x|impl §V.102(+) — add `allowed-tools` + `argument-hint` to demo-test + smoke-test SKILL.md|V102
T126|x|impl §V.102(+) description trim — lead-companies + lead-contacts `description:` to triggering intent only|V102
T127|x|add §V.101 must-sense ` ! ` ban audit recipe to check-extras.md|V101
T128|x|impl §V.42(+) _BASE pipe-table mandate in templates.py; add test asserting mandate present|V42
T130|x|impl §V.45(+) — strip §-cites from _BASE + guard test: no §[VTB] tokens in composed protocol + tool descriptions|V45
T131|x|impl §V.103(+) — TOML workflow-def; `workflow import --file` .toml + dir; migrate fixtures → `workflows/` submodule|V103,V99
T132|x|impl §V.41 + §V.45 — move workflow grounding from templates.py into workflow def instructions; delete `_DRIVE_GROUNDING`|V41,V45,V103
T137|x|impl §V.23 per §B.82 — detach OTel ctx in `_execute_task_in_worker`; fresh trace root per agent.invoke|V23,V52,B82
T138|x|add mailpilot-reply-test skill — live e2e reply-test (outbound@lab5.ca->inbound@lab5.ca, `inbound-google-drive` auto-reply, grade vs QA-Pairs.json, Logfire tokens+latency, Opus failure escalation); replaces removed test-google-drive; reply-loop guard|V104,V99,V100
T139|x|impl §V.42(+) per §B.83 — strip permissive "may use Markdown" line from `_BASE` in templates.py; extend guard test to assert `rg 'may use Markdown'` zero hits|V42,B83
T140|x|impl §V.45(+) per §B.84 — strip §-cites from the six registered tool docstrings; broaden guard test + check-extras §V.45 recipe to grep `§[VTB]\.[0-9]+` over per-tool docstrings (model-visible descriptions), not just composed protocol|V45,B84
T141|x|impl §V.102(+) per §B.85 — add `allowed-tools` + `argument-hint` to mailpilot-reply-test SKILL.md frontmatter|V102,B85
T142|x|impl §V.102(+) per §B.86 — drop `mcp__claude_ai_logfire__query_run` from mailpilot-reply-test SKILL.md `allowed-tools` (orchestrator never invokes it; Phase-4 sub-agent does, own palette)|V102,B86
T143|x|impl §V.77(+) per §B.87 — send_email recovers the existing row via get_email_by_gmail_message_id on post-send create_email ON-CONFLICT None (idempotent send), raises only when genuinely unrecoverable; add test_send_email_recovers_existing_row_on_duplicate_gmail_message_id|V77,B87
T144|x|impl §V.105(+) per §B.88 — refactor score_replies.py out-scope+compare to advisory signals; Sonnet judge as verdict-of-record for NL-shaped cases; update references/grading.md + SKILL.md scoring phase|V105,B88
T145|x|abolish runtime fact-check per §V.71 amend — remove _fact_check_body + send_email/reply_email call sites + read_ledger plumbing + reply_rejection fact_check half + check-extras fact-check recipe; numeric-spec grounding caught at test-time via reply-test grading (§V.105) not runtime|V71,V42,V105
T146|x|impl §V.106(+) per §B.89 — search_markdown tokenizes query on whitespace, per-token `fullText contains` OR-joined (raw token retained), union+dedupe by file_id, ~8-token cap; add pytest-httpx test asserting hyphenated/multi-word query emits OR predicates + unions results|V106,B89
T147|x|impl §V.103(∆) — workflow import/export TOML-only; drop JSON+stdin paths; add workflow export --account-id --out-dir|V103
T148|x|impl §V.107(+) — shared _resolve_account_id; --account-email on email send|reply + workflow create|export|import|V107
T149|x|impl §V.75(∆) per §B.90 — first sync forces full path when last_synced_at NULL|V75,B90
T150|x|impl §V.108(+) — migrations/ registry + schema_migrations ledger; db migrate applies pending; identity test|V108
T151|x|impl §V.109(+) + §V.11(∆) + §V.18(∆) — three-state schema verdict; run+mutation dead-stop on drift/pending|V109,V11,V18
T152|x|impl §V.110(+) + §I db noun — initialize_database connect+verify; db init|migrate|check CLI|V110
T153|x|impl §V.111(+) per §B.91 — strip §-cites from Click command/group docstrings + option `help=` strings in cli.py (db group + init/migrate/check, company profile help=, etc.); add guard test walking the Click tree asserting each rendered `--help` carries zero `§[VTB]\.[0-9]+`|V111,B91
T154|x|impl §V.112(+) per §B.92 — seeded_stale distinct from global stale in seed_companies.py; SKILL.md fast-path selects by arg type|V112,B92
T155|x|impl §V.113(+) per §B.93 — contact-finder.md verify step -> Bouncer real-time single GET /email/verify (per-email, <=5), drop batch/sync; empty|4xx/5xx|missing-status = verify-failure not clean unknown; add guard test grepping contact-finder.md (batch/sync absent, single-verify present)|V113,B93
T156|x|impl §V.8(∆) per §B.94 — extend ContactView w/ title + email_confidence + company_domain; verb-invariant test|V8,B94
T157|x|impl §V.96(∆) — CompanySummary + list_companies contact_count; --max-contacts/--min-contacts|V96
T158|x|impl §V.114(+) + §V.96(∆) per §B.95 — company.disabled_reason col + migration; `company disable` verb + double-disable gate; `company list` default-excludes disabled + --include-disabled; Company/CompanySummary models; lead-contacts disables company on contact-finder zero-genuine verdict (status=skipped no-DM reason, NEVER status=failed) w/ no_contacts_found:<date> at run end + SKILL.md prose|V114,V96,B95
T159|x|impl §V.108(∆) per §B.97 — `migrate_database` re-stamps `schema_metadata.schema_hash` + `mailpilot_version` to canonical hash as final step of successful migrate, re-baselines @ 0-pending when all migrations applied but recorded hash stale -> verdict `current`; regression test (init at stale hash -> migrate -> assert verdict current, not drift)|V108,B97
T160|x|impl §V.115(+) — six-family filter taxonomy + shared Click decorators (`limit_option`, `time_window_options`, `include_disabled_option`, `scope_option`, `enum_option`, `range_options`, `presence_option`); retrofit all 11 list cmds one pass — rename `--type`->`--direction`, `--route-method`->click.Choice, `--title` exact (substring -> search), add `--until` everywhere, company presence -> `--has-profile/--no-profile` tri-state|V115,V88,V20,V95,V96,V114,V111
T161|x|impl §I.cli verb+target fixes — `task retry` target positional `<task_id>`, `activity create` -> `activity add`, verb set gains `remove`|V107
T162|x|impl §V.107(∆) natural-key sweep — keyed-entity verb targets + Scope/owner options accept natural key (UUID still resolved), `(--account-id|--account-email)` pair collapses to single `--account-email`, `--company-id`/`--contact-id` -> `--company-domain`/`--contact-email`, `--company-domain` moves Text-match -> Scope, `account sync` optional `--account-email` + `--since` backfill bound (§V.75)|V107,V90,V5,V75,V94
T163|x|impl §V.116(+) tag controlled vocabulary — `tag` + `tag_assignment` two-table schema + forward migration (each distinct name -> one vocabulary row §V.90, each existing tag row -> one assignment, §V.108); redesigned `tag create|view|disable|add|remove|search|list`; `usage_count` projection; `company|contact list --tag`/`--no-tag` filters|V116,V90,V10,V108
T164|x|sweep lead-pipeline skills + `scripts/seed_companies.py` onto the standard — domain resolution -> exact `company view <domain>` (drop fuzzy `company search "<arg>" --limit 1` + `fetch_owner_name` client filter), propagate renamed flags into skill prose|V112,V96,V99
T165|x|impl §V.117(+) per §B.98 — `references/lead-pipeline-conventions.md` Batch-gate §: state distinct-batch-option rule + drop `First 25` when stale-count <= 25; sibling SKILL.md cap-example prose stays aligned per §V.100|V117,V100
T166|x|align lead-pipeline create-duplicate detection to §V.3 per §B.99 — error envelope (duplicate_key/already_exists) rides stderr not stdout: seed_companies.py classify the duplicate by exit 1 + stderr envelope not proc.stdout, correct lead-pipeline-conventions.md + contact-finder.md + sibling SKILL.md prose (drop "envelope on stdout" + "capture stdout only" for the error case)|V3,B99
T167|x|impl §V.10/§V.15/§V.80/§V.114 — `enable_company`/`enable_contact`/`enable_tag`/`enable_enrollment` in database.py + cli.py verbs; contact enable clears any reason (operator-only)|V10,V15,V80,V114,B100
T168|x|collapse enrollment to {active, disabled} — drop paused; migration 004; remove `enrollment update` + toggle path|V15,V83,V108,V88
T169|x|impl §V.118(+) + §V.79(∆) — `disable_account`/`enable_account` in database.py; disabled gate in sync loop + send/reply + `account list`|V79
T170|x|drop dead `tag_disabled` activity.type — schema.sql + models.py + migration 005|V10,V17,V108
T171|x|impl §V.96(∆) + §V.116(∆) — typed `reason_code` enum; `contacts-exhausted` tag; `--no-tag` repeatable|V96,V116,B101
T172|x|impl §V.119 — `make db-backup` target; `make clean` depends on db-backup|V119
T173|x|impl §V.105(∆) — split brittle qa-in-015 token; add `_is_brittle_inscope_token` atomicity guard|V105,B102
T174|x|impl §V.120(+) — `_sent_reply` + `AgentCompletedWithoutReplyError`; inbound-trigger enforcement|V120,B103
T175|x|impl §V.45(∆) — `_MUST_SEND` fragment in templates.py; compose into protocol_post for all three templates|V45,V120
T176|x|impl §V.121(+) + §V.119(∆) — `db export`/`db import` snapshot bundle; drop per-entity export/import|V121,V119,B104
T177|x|impl §V.75(∆) — classify 429|5xx mid-batch as retained+retried; checkpoint never advances past unstored messages|V75,B105
T178|x|impl §V.120(∆) — widen send-guard to outbound first reach-out triggers|V120,B106
T179|x|impl §V.54(∆) — absorb controlled SystemExit at `cli_mutation` boundary; parent span stays clean|V54,B107
T180|x|impl §V.7(∆) + §V.122(+) — `recipients` in EmailSummary; verify_delivery.py alias-keyed delivery verification|V7,V122,B108

## §B BUGS

id|date|cause|fix
B71|2026-06-12|`initialize_database` connect-fail path (`database.py:151`): logfire.exception w/o paired operator_event("error"), reachable from `mailpilot run` startup — operator stderr silent on DB-connect failure|V51
B72|2026-06-12|SPEC rebuild Phase 1+2 sweep excluded `.claude/skills/**` citers — 6 operative §V rows dropped, 13 cites dangled; surfaced by /sdd:check cite-DAG whole-repo scan|V22,V62,V73,V74
B73|2026-06-13|configure_logging (cli.py:66): ConsoleOptions output unset -> defaults stdout; logfire.warn lines print stdout ahead of JSON envelope, violating V3 — json.load over `mailpilot company list` fails "Extra data: line 1 column 2"|V3
B74|2026-06-13|lead-companies on theirstack.csv (stale=23): operator chose First-10 at the batch gate -> 13 rows left profile IS NULL, but run-summary envelope carries no deferred count -> operator cannot see rows need a follow-up run; symmetric in lead-contacts|V97
B75|2026-06-13|lead-companies seed collapsed accumulator catches only intra-batch apex merges; a CSV row redirecting onto a previously-seeded apex w/ a divergent name lands silent in existing — confirmed: whitecapsupply.com row named "National Concrete Accessories", surfaced only as existing:2|V98
B76|2026-06-13|`email_confidence <= N` excludes NULL (SQL three-valued logic); --max-email-confidence filter missed unknowns; operator ships unverified|V95
B77|2026-06-13|smoke-test SKILL.md documents `sync_kb_to_drive.py` + `kb-docs/` as the KB-maintenance path (Scripts :52,:63-64) + Phase 0 KB-visibility gate (:109) references the same nonexistent script — recovery command errors when KB-drift triggers it; 4th dead ref at :348|V99
B78|2026-06-14|_BASE "may use Markdown tables" (permissive) w/o pipe-table mandate; V42 lint reactive not preventive; agent generates space-aligned spec block first, lint rejects, retry — systematic 7/8 invocations under burst, retry_rate 0.875|V42
B79|2026-06-14|_BASE protocol fragment (agent/templates.py:90) embeds literal SPEC cite `(§V.42)` — runtime agent has no SPEC.md, so the §-numbering is dead authoring metadata leaking into every reply-agent system prompt|V45
B82|2026-06-15|drain worker inherits dispatching tick's OTel ctx (py3.14 ThreadPoolExecutor.submit propagates active span); co-tick agent.invoke share one trace_id|V23
B83|2026-06-15|`_BASE` carries permissive "may use Markdown" beside pipe-table mandate; check-extras rg hits non-zero; reopens §B.78 class|V42
B84|2026-06-15|six tool docstrings embed §-cites; §V.45 "tool descriptions -> zero hits" went unaudited (guard scanned composed protocol only, never per-tool docstrings)|V45
B85|2026-06-15|mailpilot-reply-test SKILL.md frontmatter omits `allowed-tools` + `argument-hint`; T125 added both to demo-test/smoke-test but the later-added skill (T138) skipped them, violating §V.102 frontmatter-hygiene|V102
B86|2026-06-15|mailpilot-reply-test allowed-tools grants query_run but orchestrator never invokes it; Phase-4 sub-agent does (own palette); zero-body-use grant|V102
B87|2026-06-15|send_email raised RuntimeError on post-send create_email ON CONFLICT None (row already carried id; cold-start race) — delivered Gmail message orphaned; §V.77 guarded Gmail-failure direction only|V77
B88|2026-06-15|_grade_outscope non-deterministic FAIL for correct polite decline — set-iteration str.replace (PYTHONHASHSEED-ordered) stranded digit substring, firing fabrication regex ~3/8 runs|V105
B89|2026-06-16|search_markdown single `fullText contains '{query}'` predicate; Drive AND-joins phrase terms -> hyphenated model `DM42-Q-FRP` returns [] -> wrongful out-of-scope decline|V106
B90|2026-06-16|renew_watches anchors gmail_history_id to watch historyId; first sync runs incremental, never full-INBOX — pre-watch INBOX mail sits below watermark, never stored/routed|V75
