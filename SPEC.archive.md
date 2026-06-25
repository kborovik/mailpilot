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
