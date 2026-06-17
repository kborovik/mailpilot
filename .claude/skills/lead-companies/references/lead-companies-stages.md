# lead-companies canonical stage recipes

Spec-of-record for the ingest, seed, and domain-resolution mechanics that
`scripts/seed_companies.py` implements verbatim (§V.72 / §V.74). The fast path in
`SKILL.md` runs the script for all of this in ONE Bash call; load this file only
when debugging a row the script reported under `skipped` (or when the script is
unavailable and a stage must be run by hand). The script is the executable
canonical; this prose is the human-readable mirror — keep them in step.

## Stage: ingest (file args)

Source- and format-agnostic ingestion. Extracts the apex domain (+ optional CSV display name per §V.72 carve-out); not profile-body content.

1. Detect format from the file's first non-empty line (peek at raw bytes, not the `Read` tool's line-numbered output):
   - Contains `,` and >=1 known header token (`domain`, `website`, `company_url`, `url`) -> **CSV mode**.
   - Else -> **plain-text mode**.
2. **CSV mode** — MUST parse with an RFC-4180 parser (`csv.DictReader`), not physical-line iteration of `Read`-tool output or split-on-`\n` / split-on-`,` (per §V.74). Quoted fields carry embedded newlines and commas (lead-export `company_description` columns) so one logical row spans many physical lines; line iteration mis-seeds prose fragments as phantom rows. Domain column auto-detect = first match in `[domain, website, company_url, url]`; operator MAY name a column in the invocation prose to override. Name column auto-detect = first match in `[company_name, name, company]` — seeds the `company.name` placeholder per §V.72 carve-out (display label only, not a profile field; enricher overwrites w/ site-canonical name). Extraction recipe — one printed line per logical row, TAB-separated `<domain>\t<display-name>` (name blank when no name-ish column):
   ```
   python3 - "$CSV_PATH" "${COLUMN:-}" <<'PY'
   import csv, sys
   path, override = sys.argv[1], (sys.argv[2] or None)
   domain_candidates = ["domain", "website", "company_url", "url"]
   name_candidates = ["company_name", "name", "company"]
   with open(path, newline="", encoding="utf-8-sig") as handle:
       reader = csv.DictReader(handle)
       columns = reader.fieldnames or []
       column = override or next((c for c in domain_candidates if c in columns), None)
       if column is None:
           sys.exit("no domain column found; name the column in the invocation")
       name_col = next((c for c in name_candidates if c in columns), None)
       for row in reader:
           value = (row.get(column) or "").strip()
           if not value:
               continue
           name = (row.get(name_col) or "").strip() if name_col else ""
           print(value + "\t" + name)  # <domain> TAB <display-name>; name MAY be empty
   PY
   ```
   `newline=""` keeps the parser in charge of embedded newlines; `encoding="utf-8-sig"` strips a leading BOM. Split each printed line on the first TAB -> `(domain, display_name)`; an empty `display_name` -> seed stage falls back to the resolved apex.
3. **Plain-text mode**: every non-empty non-comment line (skip lines starting w/ `#`): extract apex from the line (raw domain or full URL admitted). Line iteration admitted here per §V.74 — non-CSV, one domain/URL per physical line.
4. Every extracted `(domain, display_name)` pair: hand to the seed stage below (plain-text mode yields `display_name = ""`).
5. Do not pre-populate the profile JSONB body from CSV columns or text-line annotations — every seeded row lands `profile IS NULL` so agent enrichment downstream (§V.72). The CSV `company.name` placeholder (step 2) is the ONLY non-domain datum carried; all other columns (descriptions, industry, revenue, LinkedIn) discarded.

## Stage: seed (domain values)

Every value — `(domain, display_name)` from CSV ingest, or a bare domain from an inline/plain-text arg (`display_name = ""`):

1. `apex = extract_apex(domain)` — lowercase, strip leading `www.`, parse via `urllib.parse.urlsplit` if URL-shaped.
2. `resolved = resolve_apex(apex)` — follow the full redirect chain via `curl -sL -o /dev/null --max-time 12 -w '%{url_effective}' -A "Mozilla/5.0" "https://<apex>/"` (hop-agnostic, CR-free per §V.74); re-extract apex from the final effective URL if the chain ended elsewhere; else `resolved = apex`. The `display_name` travels with the row unchanged (a redirect to a sister domain does not alter the company's display label).
3. `name = display_name if display_name else resolved` (§V.72 carve-out: CSV display name when present, else the apex placeholder). `uv run mailpilot company create --domain <resolved> --name <name>` — race-safe per §V.16 (duplicate -> envelope `{"error":"duplicate_key", ...}` w/ exit 1 -> treat as existing, continue). Name is placeholder; the enricher overwrites it w/ the site-canonical name.
4. Track per-value outcome for the run summary: `{"created": [<id>...], "existing": [<domain>...], "skipped": [{"input": ..., "resolved": ..., "reason": ...}]}`.

Collision-on-resolved-apex (resolved-apex already owned by another company row, whether created earlier this run or in a prior run): skip + log per §V.72 design — operator dedups manually. Decide merge vs silent re-seed by the owner's name (§V.98): on the `create` `duplicate_key`, fetch the owning row's name (`uv run mailpilot company search <resolved>` -> match the exact-domain row). When the incoming CSV display name diverges (case- and whitespace-insensitive) from that owner name, record the collision in a `collapsed: [{"resolved": <apex>, "owner_name": <owner>, "incoming_names": [...]}]` accumulator for the run summary — a distinct-entity merge (e.g. the sole `whitecapsupply.com` row named "National Concrete Accessories" landing on a row already owned by "White Cap") silently becomes one company, and `collapsed` is the only operator-visible signal of the merge. A same-name re-seed (or a nameless plain-text/inline domain) carries no merge signal -> stays a silent `existing`. Fires both intra-batch (a name handled earlier this run) and onto a previously-seeded row (owner name fetched from the DB).

## Domain extraction & redirect resolution

```
extract_apex(url_or_domain):
    1. parse -> host (urllib.parse.urlsplit)
    2. lowercase
    3. strip leading "www."
    4. return host (no further subdomain stripping)

resolve_apex(initial):
    # -w '%{url_effective}' prints the final URL after the full redirect chain,
    # CR-free (per spec V.74). It is always set, so when there is no redirect
    # the final apex equals `initial`. A HEAD-based location-header grep is
    # brittle: 403 bot-blocking origins answer HEAD differently, and awk
    # retains the header's trailing CR -> corrupts a bare-host redirect target.
    final_url = curl -sL -o /dev/null --max-time 12 -w '%{url_effective}' \
                -A "Mozilla/5.0" "https://<initial>/"
    return extract_apex(final_url) if final_url else initial
```

Subdomain preservation: `shop.acme.com` not collapsed to `acme.com` — preserves distinct entity identity if shop is a separate company row.

## Profile shape

The enrichment target. Stored as `company.profile JSONB NULL`, validated server-side via the `CompanyProfile` Pydantic model (`src/mailpilot/models.py`); the field semantics and required/optional split are authoritative in §V.72 — this is the operator-facing gloss:

```yaml
summary: str           # 1-5 sentences -- what they do, who they serve, hook
products: list[str]    # what they sell -- pitch relevance
target_customers: str  # who they sell to -- ICP signal
timezone: str | None   # IANA, e.g. "America/Toronto"; null if multi-zone or unclear
sources: list[str]     # >=1 url fetched/cited
```

Required = {summary, products, target_customers, sources}. Optional = {timezone}. Invalid JSON -> CLI rejects w/ `{"error":"validation_error", ...}` envelope per §V.54.
