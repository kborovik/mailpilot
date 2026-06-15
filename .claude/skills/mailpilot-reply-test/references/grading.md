# Grading rubric

How `score_replies.py` turns a captured reply body into PASS / FAIL / NO_REPLY.
All matching is whitespace-normalized (`\s+` → single space) and case-insensitive
unless noted. Grading is intentionally deterministic and code-only — no LLM — so
results are stable and cost nothing.

## in-scope

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

## out-of-scope

The product is not in the KB; the agent must decline without inventing specs.
PASS iff **both**:

1. **No fabrication** — no `forbidden_token_pairs` co-occur. Each pair is
   `[brand, regex]` and "fires" when the brand string appears in the body AND
   the regex (e.g. `\d`) matches — i.e. the agent quoted a number for an absent
   product. The regex is matched against the body *minus question-echoed digits
   and URL hosts* (a polite decline routinely restates the asker's own figures
   and links a referral page, neither of which is a fabricated spec), so only
   numbers the agent invented count. Any fire ⇒ FAIL (`detail.fabrication_hits`).
2. **Declined** — at least one `decline_signals` phrase is present
   (`detail.decline_signals_found`).

## compare

Hardest to grade deterministically, so this is a **structural proxy** aligned
with the workflow's own rules ("cite the source file name", "present specs as a
GFM pipe table"). PASS iff all of:

- **Cited** — every `must_cite` source file is referenced, matching the filename
  with or without the `.md` suffix (`detail.cited`).
- **Mentioned** — every `must_mention` model id is present (`detail.mentioned`).
  `must_mention` is derived in `select_cases.py` from the primary token of the
  in-scope case that shares each source file; only compare cases whose targets
  all have an id-like (`contains a digit, no spaces/colons`) primary token are
  eligible, so this check is reliable.
- **Table** — the body contains a GFM table separator (`|---`).

This proves the reply is grounded, cites its sources, names the right products,
and is formatted as required. It does **not** verify that every compared number
is numerically correct — that deeper check is left to the human reading the
report or to the Logfire-backed analysis, and this scope is stated honestly
rather than overclaimed.

## no reply

If `collect_replies.py` saw no threaded reply before the timeout, the case is
`NO_REPLY`. This is a real failure mode worth surfacing — typically the
classifier did not route the email to the workflow, the agent errored, or the
run loop was not draining tasks — and it triggers the Opus investigation.
