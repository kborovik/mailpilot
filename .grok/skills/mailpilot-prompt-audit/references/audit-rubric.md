# Prompt-composition audit rubric

The unit of critique is the **authored prompt text** — the code-defined system
prompt (template fragments in `agent/templates.py`, the classifier
`_INSTRUCTIONS` in `agent/classify.py`) and the workflow's own TOML `goal` and
`instructions`. Score each system against the dimensions below, using the
Logfire telemetry as evidence, then propose concrete edits.

Read SPEC.md §C harness-over-LLM first — "minimal decision surface",
"system-driven not agent-driven", and "simplicity above all" are the lens.

## Dimensions (score each 1-5)

1. **Clarity and non-contradiction.** Every directive is unambiguous. No two
   lines pull in opposite directions, and nothing in `workflow.instructions`
   contradicts a code-defined fragment (e.g. an instruction that softens the
   no-fabrication rule, or one that lets a turn end without a send).

2. **Minimal decision surface.** The prompt narrows the agent to one scoped
   choice per turn rather than presenting many. Multi-step side-effects are
   bundled behind a single tool instruction; the palette is constrained to the
   decision at hand. Over-long instructions that re-explain mechanics the
   harness already enforces widen the surface for no gain.

3. **Grounding discipline.** Search-first, cite-the-source, decline-when-unsure,
   and no-fabrication are explicit and mutually reinforcing. For knowledge-base
   workflows the grounding rules live in `instructions` (§V.41), so check they
   are actually present and specific, not assumed from the template.

4. **Tool-use contract coherence.** The instructions never ask for an action
   that bypasses the send obligation (`_MUST_SEND`, §V.120) or the one-tool-call
   decline path (`_DECLINE`). The spec-table mandate (`_BASE`, §V.42) is
   reinforced, not undercut, by the workflow's own formatting guidance.

5. **Token economy and cache-friendliness.** Instruction length is justified by
   value. The stable prefix (template protocol + instructions) is the cached
   span (§V.47), so churn-prone or redundant text raises cost on every turn.
   Flag instruction text that merely restates a code fragment — duplication
   wastes tokens and invites drift.

6. **Redundancy and drift versus code fragments.** A workflow instruction that
   duplicates `_BASE` / `_NO_FABRICATION` / `_MUST_SEND` risks diverging from
   the canonical rule over time. Recommend deleting the duplicate or, if the
   rule belongs to every workflow, lifting it into the fragment (a code change
   plus PR per §V.44).

7. **Goal quality for routing.** `workflow.goal` is the only text the classifier
   matches on. Judge it as a routing signal: is it crisp and discriminative, or
   vague and overlapping with a sibling workflow's goal? Overlap shows up as
   misrouting in the classifier telemetry.

8. **Empirical alignment.** Tie each telemetry signal back to wording:
   - High **tool-error rate** → tool guidance is unclear or the palette invites
     bad arguments.
   - Low **cache-read share** → the prompt prefix is unstable, or instructions
     interpolate per-run data that should live in the user prompt.
   - High **tokens per invocation** with short prompts → the agent is looping;
     check whether instructions over-encourage tool fan-out.
   - **failed** runs or **no-send** completions → the send/decline contract is
     not landing.
   - Classifier **no_match** spikes → goals are not discriminative.

## Edit-suggestion format

Each suggested improvement must carry:

- **target** — one of: `code:templates.py:<fragment>`, `code:classify.py`,
  `toml:<workflow> goal`, `toml:<workflow> instructions`. Code targets are a
  change plus PR (§V.44), not a workflow update — say so.
- **current** — the exact line or short block being changed (quoted).
- **proposed** — the replacement wording.
- **evidence** — the telemetry signal or composition observation that motivates
  it (name the metric and value, or the contradicting lines).
- **confidence** — high | medium | low.
- **priority** — the first edit is the single highest-impact change.

Suggestions are advisory. This skill recommends; it never edits the prompts.
