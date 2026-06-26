# Reply-handling wording critique rubric

This rubric guides the Opus critique sub-agent. The unit of critique is the
**workflow wording** -- the `goal` and `instructions` in the workflow TOML
that drove the agent, in particular its "Handling replies" section. It is not the
individual replies. The agent's branch behavior across the scenarios is evidence
of what that wording produces, nothing more.

The aim is to **suggest changes and improvements to the workflow wording**.
The operator edits the workflow, re-runs the test, and the next run's reply
handling improves. So every finding must point back to a line of the `goal`
or `instructions`, and every suggestion must be an edit an operator can paste into
the workflow TOML.

## How to read the input

`critique_input.json` carries:

- `workflow` -- the `name`, `goal`, and `instructions` under test. This is
  the artifact you are judging. Focus on the reply-handling directives (the
  branches: positive/booked, question, not-now, opt-out, auto-reply, wrong
  person), the booking mechanism, and the grounding constraints.
- `company` -- the grounding company the Touch 1 was personalized against.
- `scenarios` -- one record per reply branch. Each carries the crafted
  `inbound_reply`, the agent's `agent_reply`, the `expected_branch`, the
  `observed` outcome state (terminal outcome, whether the contact was disabled,
  whether the agent replied), and `pass` plus `notes`. This is the evidence.

## The wording-vs-tool gap

The workflow's "Handling replies" prose names outcomes like "completed, reason
opt-out" and "completed, reason not-now". The agent's actual terminal tool
records a `failed` outcome for the disable branches (opt-out, wrong person) and
for `contact_later`. When the prose and the tool disagree, call it out as a
wording weakness: the wording instructs an outcome the tooling cannot produce, so
the operator's mental model of the timeline is wrong. Suggest wording that matches
what the tool actually records.

## Grounding rule

- Tie every finding to the scenario evidence, not to a guess. "The opt-out reply
  was acknowledged but the not-now reply was not" is a wording finding when the
  instructions treat the two branches differently. A one-off phrasing quirk in a
  single reply is an agent draw, not a wording problem.
- When a branch behaves correctly and the wording clearly caused it, say so. A
  strength tells the operator what not to touch.

## Dimensions

Score each dimension 1 to 5 (1 = the wording reliably produces a poor branch
result, 3 = competent, 5 = the wording reliably produces an excellent result).

1. **Branch coverage.** Does the wording name every reply branch the inbound mix
   produces (interest, question, objection, opt-out, auto-reply, wrong person),
   and does the agent route each reply to the right one? Missing or ambiguous
   branch directives score low.
2. **Terminal-outcome correctness.** Does the wording tell the agent to conclude
   the enrollment with the right disposition, and does the recorded outcome match
   what the wording intends (see "The wording-vs-tool gap")? Wording that records
   the wrong outcome, or none when one is due, scores low.
3. **Booking mechanism.** For the positive branch, does the wording produce a
   clean, link-only booking (the calendar link, no invented times)? A booked
   branch that invents availability or omits the link scores low.
4. **Grounding and honesty.** Does the wording keep replies grounded in the
   lab5.ca message and the company record, declining to invent facts, figures, or
   availability? Replies that fabricate score low.
5. **Consent and safety.** Does the opt-out branch reliably disable the contact,
   and the wrong-person branch mark the contact bad, without over- or
   under-reacting (e.g. disabling on a mere question)? Weak consent handling
   scores low.
6. **Tone in replies.** Are the handling replies direct, brief, and human, matching
   the cold-email register without hype or filler? Weak tone constraints score low.
7. **Cadence guardrails.** Does the wording correctly stop the cold cadence on a
   reply (no follow-up after engagement) and noop on an auto-reply without burning
   a touch? Missing guardrails score low.

## Overall score

Give one holistic overall score 1 to 5 for the reply-handling wording. It is a
judgment, not a strict average: a wording gap that reliably produces a fatal
branch error (failing to disable on opt-out, inventing a meeting time, replying to
an auto-reply and burning a touch) caps the overall score at 2 even if other
directives are strong.

## Output

Produce one critique of the workflow's reply-handling wording, not one per reply:

- `strengths` -- one to three wording directives that work, each named with the
  scenario evidence that proves it.
- `patterns` -- one to three patterns across the branches, each traced to the line
  of the `goal` or `instructions` that causes it.
- `weaknesses` -- one to three wording gaps or flaws, each tied to a scenario's
  evidence (include the wording-vs-tool gap if present).
- `edits` -- one to three concrete edits to the `goal` or `instructions`,
  phrased so an operator can paste them in. The first edit must be the single
  highest-impact change. Quote or name the line to change and give the replacement
  wording.

Keep every line specific to this workflow. Do not restate the rubric, and do not
rewrite individual replies.
