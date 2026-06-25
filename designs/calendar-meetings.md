# Calendar operations: meetings conclude enrollments

## Problem

The current PR cancels an enrollment's pending follow-up tasks when an inbound
reply routes to it (§V.123, §T.182, §B.110). Two gaps remain. A prospect can
book a Google Meet through the lab5.ca website without replying, and the system
has no path to detect that booking or treat it as a stop signal. An enrollment
also carries a goal — for example, book a Google Meet — and nothing concludes
the enrollment when the goal is met or refused, so the cold sequence keeps
running.

A second constraint shapes the solution. Cheap models handle one narrow decision
well and many decisions poorly, and Opus costs too much for routine turns. So the
system owns deterministic lifecycle logic and hands the tactical agent the
smallest possible decision (new CLAUDE.md principles: system-driven, minimal
decision surface).

## Proposal

Model a meeting as a first-class entity, peer to email. New `meeting` table keyed
on `google_event_id` (nullable-unique, idempotent ingest, mirrors
`email.gmail_message_id` §V.90). Columns: `id`, `google_event_id`, `meet_url`,
`summary`, `scheduled_at`, `ends_at`, `status` CHECK in {scheduled, completed,
cancelled, no_show}, `created_at`, `updated_at`. A meeting links to one or more
contacts through a `meeting_attendee(meeting_id, contact_id)` link table, UNIQUE
per pair (mirrors `tag_assignment` §V.116). The `status` column is operator
record-keeping only and gates nothing.

New `CalendarClient` in `calendar.py`, mirror shape of `GmailClient` and
`DriveClient` (§I). Service account plus domain-wide delegation, per-account
`with_subject(email)`. Scope `calendar.events.readonly`. The sync loop polls
upcoming events on the run-interval tick, upserts one `meeting` row per event
idempotently on `google_event_id`, and links attendees matched by email to
contacts.

Booking concludes enrollments deterministically, no agent turn. For each attendee
contact with an active outbound enrollment, the system concludes the enrollment
via `record_enrollment_outcome` (§V.15), cancels its pending future follow-up
tasks via `cancel_enrollment_followup_tasks` (§T.182), and writes a system
booking note. Conclusion fires for every active outbound enrollment the attendee
holds — a booked meeting outranks any cold sequence.

Bundle the agent's terminal decision behind one tool. New agent tool
`conclude_enrollment(disposition, note, reschedule_at)`, disposition in
{meeting_booked, do_not_contact, contact_later}. The tool counts as a valid
send-obligation terminal like `noop` (§V.120). The system runs the side effects
per disposition:

- meeting_booked leads to conclude plus cancel follow-ups plus note (the path
  when a prospect writes "I booked", distinct from calendar detection).
- do_not_contact leads to conclude plus cancel follow-ups plus `disable_contact`
  (§V.79, §V.80) plus note.
- contact_later leads to conclude plus cancel follow-ups plus a scheduled
  re-enrollment task at `reschedule_at` (agent-supplied, default at least 3
  months out when omitted) plus note. A scheduled task self-fires when the date
  arrives; an inert timestamp column would need Claude Code to poll it.

`conclude_enrollment` is the single agent-facing terminal tool.
`record_enrollment_outcome` becomes internal, no longer in the agent tool set.
The agent's whole decision on an inbound reply is reply, or conclude with one
disposition plus a note.

`cancel_enrollment_followup_tasks` (§T.182) now fires from three sites: inbound
reply routing (§V.123), calendar booking ingestion, and `conclude_enrollment`.
The first-touch exclusion (context trigger enrollment_schedule §V.32) holds at
every site.

## Layering and ownership

The system owns every lifecycle decision as deterministic code: calendar
ingestion, booking conclusion, follow-up cancellation, suppression, scheduled
re-enrollment. The tactical agent owns only the judgment calls: classify the
reply, draft the reply body, pick one terminal disposition, write a note. Claude
Code (strategic layer) owns nothing new at runtime — calendar reading moved into
the app, not the MCP layer, because the system must act on bookings
deterministically.

The Pydantic agent tool palette stays lean. It loses `record_enrollment_outcome`
and gains `conclude_enrollment` — net zero count, and the model now faces one
terminal tool instead of orchestrating outcome plus disable plus note across
three calls.

## Effect on in-flight SPEC items

- §C amended — add Google Calendar scope `calendar.events.readonly`. The first
  scope added beyond Gmail `gmail.modify` and Drive `drive.readonly`.
- §I amended — new `calendar.py` module exporting `CalendarClient`; new `meeting`
  CLI noun (verbs list, view, add, update, cancel); agent tool set gains
  `conclude_enrollment`, drops agent-facing `record_enrollment_outcome`.
- §V.123 and §T.182 — `cancel_enrollment_followup_tasks` reused from two new call
  sites (calendar booking, `conclude_enrollment`), unchanged logic, first-touch
  exclusion preserved.
- §V.15 — `record_enrollment_outcome` semantics intact; the function moves out of
  the agent tool set into system-internal use.
- §V.21 — run-interval poll for calendar is a timer-based fallback, not an event
  wake. Tracked as a known gap (GitHub issue plus Out of scope).
- §V.79, §V.80 — `disable_contact` reused for the do_not_contact disposition.
- §V.120 — `conclude_enrollment` joins `noop` as a valid terminal that satisfies
  the send obligation.
- New §V invariants — meeting entity plus attendee linkage; CalendarClient
  ingestion plus idempotency; bundled `conclude_enrollment` plus disposition side
  effects; booking conclusion fan-out across active outbound enrollments.
- New §T rows — schema (meeting plus meeting_attendee plus migration),
  CalendarClient plus sync ingestion, `conclude_enrollment` tool, `meeting` CLI
  noun.

## Design decisions

- **Decision:** Model the meeting as a first-class table peer to email, with a
  many-to-many `meeting_attendee` link. **Why:** mirrors the Gmail and Drive
  client pattern and the email entity (google id plus linkage); a Google Meet can
  carry several attendees, so many-to-many is the honest shape, and the link
  table is cheap insurance against a later migration.
- **Decision:** App-runtime `CalendarClient` reads Google Calendar; ingestion is
  deterministic system code. **Why:** the system must act on a booking
  deterministically (conclude, cancel) the moment it lands; routing the read
  through Claude Code MCP would put an LLM in a lifecycle path the system-driven
  principle reserves for code.
- **Decision:** One bundled `conclude_enrollment` tool with a disposition enum;
  `record_enrollment_outcome` goes internal. **Why:** the minimal-decision-surface
  principle — a cheap model picks one disposition rather than orchestrating
  outcome plus disable plus note across three calls.
- **Decision:** `contact_later` creates a scheduled re-enrollment task, not a
  contact timestamp column. **Why:** a scheduled task self-fires when the date
  arrives; an inert `contact_after` column would need Claude Code to poll and act
  on it.
- **Decision:** A booked meeting concludes all the attendee's active outbound
  enrollments. **Why:** a real meeting outranks every cold drip to that person,
  regardless of which workflow drove it.
- **Decision:** `contact_later` interval is agent-supplied with a 3-month default
  when omitted. **Why:** the agent reads the reply context (for example, "reach
  out after Q3") yet a sensible default keeps the date optional.
- **Decision:** Calendar ingestion polls on the run-interval tick initially.
  **Why:** Calendar push channels (watch plus webhook) are materially more
  infrastructure; the poll ships the feature, and the §V.21 event-wake gap is
  tracked in a GitHub issue.
- **Decision:** Meeting `status` is operator record-keeping only, decoupled from
  enrollment conclusion. **Why:** conclusion fires at booking because the goal is
  the booking; whether the meeting later completes or no-shows is a separate
  operator concern with no lifecycle gate.

## Success criterion

- A Google Meet booked through lab5.ca, with the prospect's email as attendee,
  produces a `meeting` row, cancels that contact's active enrollments' pending
  future follow-up tasks, and writes a booking note — with no inbound reply and
  no agent turn.
- Re-polling the same calendar event creates no duplicate `meeting` row
  (idempotent on `google_event_id`).
- An inbound refusal reply lets the agent conclude the enrollment in one
  `conclude_enrollment` call that disables the contact or schedules a
  re-enrollment task and writes a note; the agent calls no other terminal tool.
- A meeting with two attendee contacts links both through `meeting_attendee` and
  concludes both their active enrollments.
- The agent tool set exposes `conclude_enrollment` and not
  `record_enrollment_outcome`.

## Out of scope

- Calendar push channels (watch plus webhook) for event-driven ingestion —
  deferred, tracked in #154; run-interval poll ships first (§V.21 gap).
- Meeting outcome workflow beyond the operator note plus the §V.32 scheduled
  follow-up — no structured post-meeting status transition drives automation.
- Multi-contact conclusion nuance beyond "conclude every attendee's active
  outbound enrollments".
- Calendar write operations — scope stays `calendar.events.readonly`, no event
  creation or update from the app.
