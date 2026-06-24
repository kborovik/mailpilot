# Workflow-wording critique rubric

This rubric guides the Opus critique sub-agent. The unit of critique is the
**workflow wording** -- the `objective` and `instructions` in the workflow TOML
that drove the agent. It is not the individual emails. The sent emails are
evidence of what that wording produces, nothing more.

The objective is to **suggest changes and improvements to the workflow wording**.
The operator edits the workflow, re-runs the test, and the next batch of emails
improves. A per-email rewrite is wasted: those emails are already sent, and the
next run draws fresh ones from the same wording. So every finding must point back
to a line of the `objective` or `instructions`, and every suggestion must be an
edit an operator can paste into the workflow TOML.

## How to read the input

`critique_input.json` carries two things:

- `workflow` -- the `name`, `objective`, and `instructions` under test. This is
  the artifact you are judging.
- `emails` -- one record per sent email, each with the recipient's contact and
  company context, the agent-written subject, and the body. This is evidence.

Read the `instructions` first. Then read the emails as a set, looking for what
the wording caused: a directive that worked, a directive every email ignored, a
gap the wording never covered, an instruction that pushed the agent toward a weak
pattern.

## Grounding rule

- Tie every finding to evidence across the emails, not to one email in
  isolation. "Every email buried the offer below the company history" is a
  wording finding. "Email 3 has a weak subject" is not -- unless the subject
  rule in the wording explains why, and the pattern repeats.
- A pattern that shows up in one email out of nine is an agent draw, not a
  wording problem. Report it only if you can trace it to a specific instruction.
  A pattern in most or all emails is a wording problem -- name the line that
  causes it.
- When the wording already constrains something well and the emails honor it,
  say so. A strength tells the operator what not to touch.

## Dimensions

Score each dimension 1 to 5 (1 = the wording reliably produces a poor result,
3 = competent, 5 = the wording reliably produces an excellent result). The score
rates the **wording's directives**, judged by what the emails show.

1. **Message clarity.** Does the wording state the core message (problem, fit,
   offer, proof) so the agent reproduces it accurately and consistently? Vague or
   contradictory directives that let emails drift score low.
2. **Personalization directives.** Does the wording force grounding in the
   specific contact and company, or does it permit mail-merge output? If the
   emails read as a template with a name swapped in, the personalization
   directives are too weak.
3. **Value-proposition framing.** Does the wording make the agent lead with a
   concrete, specific benefit, or does it let the offer sit under company
   history? Directives that produce buried or adjective-heavy value score low.
4. **Structure and length.** Do the structural and length rules produce
   skimmable, correctly sized emails? Rules that produce walls of text, padding,
   or fused ideas score low.
5. **Subject directives.** Do the subject rules produce specific, honest,
   non-spammy subjects? Rules that allow vague, clickbait, or near-identical
   subjects score low.
6. **Tone and constraints.** Do the tone rules keep emails direct and human
   without hype, filler, or over-familiarity? Missing or weak tone constraints
   score low.
7. **Deliverability guardrails.** Do the formatting and content rules steer the
   agent away from spam triggers, broken rendering, and risky formatting? Missing
   guardrails that let spammy or malformed output through score low.

## Overall score

Give one holistic overall score 1 to 5 for the workflow wording. It is a
judgment, not a strict average: a wording gap that reliably produces a fatal
email flaw (mail-merge bodies, a missing or vague call to action, spam-trigger
phrasing) caps the overall score at 2 even if other directives are strong.

## Output

Produce one critique of the workflow wording, not one per email:

- `strengths` -- one to three wording directives that work, each named with the
  evidence across the emails that proves it.
- `patterns` -- one to three patterns the emails share, each traced to the line
  of the `objective` or `instructions` that causes it.
- `weaknesses` -- one to three wording gaps or flaws, each tied to the evidence
  across the emails.
- `edits` -- one to three concrete edits to the `objective` or `instructions`,
  phrased so an operator can paste them in. The first edit must be the single
  highest-impact change. Quote or name the line to change and give the
  replacement wording.

Keep every line specific to this workflow. Do not restate the rubric, and do not
rewrite individual emails.
