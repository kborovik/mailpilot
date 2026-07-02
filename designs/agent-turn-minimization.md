# feat(agent): mechanical context pre-feed and system-owned touch cadence

## Problem

A Touch 1 send costs 5 model calls where only one exercises judgment. Trace
`019f216c0f34c5a2b915855ca7ef6f08`: 56,156 input tokens, 5,850 output, 66
seconds, about $0.18. Calls 1-2 fetch ContactView and CompanyView --
deterministic reads the harness already holds keys for. Call 4 executes a fixed
cadence rule stated as prose. Call 5 re-bills 15k tokens to emit a 107-token
summary. Each round trip after compose re-pays the 5.3k thinking tokens as
fresh input.

Prompt-fidelity failures confirm the structural gap: the agent read the lab5.ca
company record although the instructions forbid it; defects in decisions the
system can own recur per §B.120, §B.111, §B.106. The Touch 2/3 reply self-guard
burns a full agent run on a pure database predicate. A 355-send first-touch
wave is pending in production (scheduled from 2026-07-02 13:00 UTC) -- at 5
calls per send versus 1, the wave alone covers the change's cost.

## Proposal

Four parts, independently landable, ordered by risk:

**1. Context pre-feed.** `invoke_workflow_agent` loads
`load_contact_view(contact.id)` and, when `company_id` is set,
`load_company_view(company_id)`, rendering both as `Contact record:` /
`Company record:` JSON sections in `_build_user_prompt`. Same loaders the tools
used, so agent and operator context stay byte-identical (§V.8). `read_contact`
and `read_company` leave all rosters; `_BASE` drops its read-tools sentence
(§V.40 audit: the reworded fragment must not name exactly one tool).

**2. System-owned touch cadence.** Workflow definition gains cadence def fields
(`touches = 3`, `touch_interval_days = 7`), nullable in schema -- NULL means
single-touch, no automatic follow-up. Weekend-roll-to-Monday is cadence-engine
code. After a successful touch-N send the harness creates the touch-N+1 task
with context `{touch: N+1, prior_email_id}`; after the final touch it concludes
the enrollment `contact_later` ("sequence exhausted") through the
system-internal path calendar booking already uses (§V.128 precedent). Cadence
and after-a-touch prose leave the TOML. `create_task` stays bound for
reply-branch soft follow-ups.

**3. Compose-only agent shape for touch runs.** Touch triggers build the agent
with `output_type = TouchMessage {subject: str | None, body: str}` and zero
tools; the harness lints (§V.42 regex as output validator, bounded
`ModelRetry`), sends via `email_ops`, schedules the next touch. One LLM call
per touch. The send obligation becomes structural for touches (§V.120). Reply
and deferred-task triggers keep the tool loop with the trimmed palette.

**4. Deterministic touch pre-flight.** `execute_task` gains two guards in the
§V.83 chain, scoped to touch tasks: latest enrollment outcome terminal, or an
inbound email from the contact after the prior touch -- either cancels the task
with zero LLM calls. Complements §V.123 for unrouted and racy replies. Reply
self-guard prose leaves the TOML.

Expected effect per touch: 5 calls become 1; input drops from about 56k to
about 10k tokens; cost from about $0.18 to about $0.09. Stale follow-ups drop
to zero calls.

## Agent shape per trigger

Template registry stays the single shape owner (§V.44). Dispatch is
trigger-keyed: `context.touch` present, or `trigger = enrollment_schedule`, or
first-reach-out CLI triggers lead to the compose-only shape; outbound
`task`/`email` triggers lead to the tool-loop shape; inbound unchanged.
`_DEFERRED_TASK_INITIAL` retires: touch turns bind no tools to misgovern. §V.81
gains a structured-output exemption -- the validated output is the action.

## Cutover

One-time rewrite at the migration boundary; no runtime prose parsing, no
dual-path period.

1. **Migration 010** (single deploy unit, §V.108): adds the nullable cadence
   columns, then rewrites pending legacy touch tasks in SQL --
   `UPDATE task SET context = context || jsonb_build_object('touch',
   substring(description FROM '^Touch ([0-9]+)')::int) WHERE status = 'pending'
   AND description ~ '^Touch [0-9]+ of [0-9]+'`. The parse lives and dies
   inside the migration. Production on 2026-07-02: exactly 44 such rows; the
   other 355 pending rows already carry `trigger = enrollment_schedule` and
   dispatch as touch 1 with no rewrite.
2. **prior_email_id resolution**: read from task context when present; when
   absent, resolve deterministically as the enrollment's latest outbound email
   (all touches share one thread). Removes dependence on what the old agent
   happened to store.
3. **Deploy runbook**: stop `run`, then `db migrate`, then
   `workflow import --file workflows/` (TOML now carries cadence fields), then
   `workflow check` green as the gate (§V.134), then start `run`. The import
   lands before any task drains, so no touch ever executes against undefined
   cadence in a normal deploy.
4. **Belt**: a touch task with `touch >= 2` draining against NULL cadence
   (import skipped or late) reschedules +1 hour with an operator warn,
   mirroring the lock-contention shape (§V.25) -- nothing sends into an
   undefined sequence, nothing is lost, and it self-heals after import. Touch 1
   against NULL cadence sends and schedules nothing -- that is single-touch
   semantics, not an error.

## Effect on in-flight SPEC items

- §I agent tools: `read_contact`, `read_company` removed (15 tools become 13).
- §V.8 amended: tool-parity clause becomes prompt-parity (shared loaders
  unchanged).
- §V.31 and §V.45 amended: `_DEFERRED_TASK_INITIAL` retires; fragment chain
  shrinks; §V.40 audit on reworded `_BASE`.
- §V.81 and §V.120 amended: structured-output exemption; touch send obligation
  structural.
- §V.83 amended: pre-flight gains outcome-terminal and replied-since-prior-touch
  guards.
- §V.103 amended: def fields gain cadence numerics; import/export and
  `workflow check` hash cover them.
- §V.108: migration 010 carries the schema change plus the one-time
  task-context rewrite.
- §V.127 amended: system concludes sequence exhaustion; agent terminal
  unchanged for replies.
- New §V rows: mechanical context pre-feed; system-owned touch cadence.
- §B.106, §B.111, §B.118, §B.120 recurrence classes structurally narrowed.

## Design decisions

- **Decision:** drop `read_contact`/`read_company` from all templates, inbound
  included. **Why:** inbound personalizes from the same two views and routing
  already resolved the contact; one prompt shape for both directions, and the
  wrong-record fetch class dies everywhere.
- **Decision:** cadence policy lives in TOML def fields, not code defaults.
  **Why:** cadence is campaign policy; §V.103 already owns definition fields
  with import, export, and `workflow check` integrity -- a code default would
  silently activate cadence for every outbound workflow.
- **Decision:** full compose-only structured output for touch runs. **Why:**
  after the pre-flight guard, a touch turn holds exactly one judgment -- the
  message text; zero tools makes the send obligation structural (§V.120) and
  removes every misgovernable choice (§B.120 class).
- **Decision:** cutover via one-time migration rewrite, no runtime
  parse-fallback. **Why:** the parse exists once, in migration 010, against a
  verified population (44 rows); runtime dispatch keys only on structured
  context, so prose never steers execution again.

## Success criterion

- A Touch 1/2/3 run emits exactly one `chat` span; a stale follow-up emits zero
  (Logfire-checkable).
- `read_contact`/`read_company` spans no longer occur; the prompt carries both
  record sections whenever the rows exist.
- A replied or concluded enrollment can never receive a later touch, even when
  routing missed the reply.
- Touch `scheduled_at` values are system-computed only; no agent-supplied
  timestamps on touch tasks.
- After `db migrate`, zero pending tasks match `^Touch [0-9]+ of [0-9]+`
  without `context.touch`.

## Out of scope

- `anthropic_effort` tuning for compose (measure with the campaign-test skill
  after the structure lands).
- Removing `list_enrollments`/`cancel_task` from rosters.
- Inbound Drive KB retrieval (query-dependent; stays a tool loop).
