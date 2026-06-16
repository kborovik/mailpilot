# Grading rubric

How a captured reply body becomes PASS / FAIL / NO_REPLY. Two verdict sources,
split by case type (§V.105):

- **in-scope** — graded deterministically by `score_replies.py` (code-only, no
  LLM): cheap, stable, false-PASS at worst, never a false FAIL.
- **out-of-scope + compare** — these are free-form natural-language replies
  (a polite decline, a cross-datasheet comparison). Deterministic substring /
  regex grading of them yields false and seed-unstable verdicts (§B.88), so
  `score_replies.py` no longer decides them: it emits **advisory signals** and a
  `"JUDGE"` sentinel verdict, and a **Sonnet judge sub-agent** (SKILL.md step 3b)
  is the verdict of record. The judge reads the reply body, the case rubric, the
  advisory signals, and — for compare — the source datasheets, and returns
  `{verdict, rationale}`. `apply_judgments.py` folds that verdict back into the
  scoring file.

All deterministic matching is whitespace-normalized (`\s+` → single space) and
case-insensitive unless noted.

## in-scope (deterministic)

The reply must be grounded in the one source datasheet. PASS iff **every**
`expected_tokens` entry is a substring of the body.

- `detail.token_hits` records each token's presence; `detail.missing` lists any
  absent ones.
- **Selection guard**: `select_cases.py` only picks in-scope cases with ≥2
  specific tokens (length ≥ 5), and Run-A additionally requires *all* tokens to
  be length ≥ 4. This avoids a known weakness — a bare token like `"3"` is a
  substring of almost any reply, so it can mask a wrong answer (false PASS). It
  never causes a false FAIL. The specific tokens (model numbers, units like
  `"0.48 mm"`, capacities like `"260,000 GPD"`) carry the real signal.

## out-of-scope (judge verdict, advisory signals)

The product is not in the KB; the agent must decline without inventing specs.
`score_replies.py` emits, in `detail`:

- `fabrication_candidates` — each `forbidden_token_pairs` entry `[brand, regex]`
  whose brand string appears in the body **and** whose regex (e.g. `\d`) matches
  somewhere in the body. This is a *candidate*, not a verdict: a polite decline
  routinely names the absent product and restates the asker's own figures or
  links a referral page, so a brand-near-a-digit is not necessarily a fabricated
  spec. The judge reads the actual reply and rules.
- `decline_signals_found` — which `decline_signals` phrases are present.

The judge PASSes a reply that clearly declines and invents no spec for the absent
product, FAILs one that fabricates a number or fails to decline.

## compare (judge verdict, advisory signals)

The reply must compare the named products grounded in both datasheets, cite its
sources, and present the specs as a GFM pipe table. `score_replies.py` emits, in
`detail`:

- `token_hits` — presence of each `must_cite` source file (matched with or
  without the `.md` suffix) and each `must_mention` model id.
- `has_table` — whether the body contains a GFM table separator (`|---`).

The judge reads the reply and the two source datasheets and rules on the deeper
question a structural proxy cannot answer: whether the compared numbers are
actually correct and grounded, on top of citation and formatting.

## no reply

If `collect_replies.py` saw no threaded reply before the timeout, the case is
`NO_REPLY` (recorded by `score_replies.py`, judge not invoked). This is a real
failure mode worth surfacing — typically the classifier did not route the email
to the workflow, the agent errored, or the run loop was not draining tasks — and
it triggers the Opus investigation.
