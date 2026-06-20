#!/usr/bin/env python3
"""Consolidated ingest + seed for the lead-companies skill.

Collapses the skill's per-row Bash loop -- format detect, CSV/text parse,
apex redirect resolution, and `mailpilot company create` per domain -- into a
single tool call, then captures the post-seed stale-row set so the enrich
Workflow can be dispatched without a second `company list` round trip.

Faithful to the spec recipes this skill is built on:
  - SPEC.md V.72: external sources contribute the apex domain + an optional CSV
    display-name placeholder ONLY. No profile-body field is pre-populated here;
    every seeded row lands `profile IS NULL` for downstream agent enrichment.
  - SPEC.md V.74: CSV ingestion uses an RFC-4180 parser (csv.DictReader), never
    physical-line iteration; redirect resolution uses the hop-agnostic,
    CR-free `curl -sL -o /dev/null -w '%{url_effective}'`, never a HEAD grep.

Usage:
    seed_companies.py [--dry-run] [--column NAME] <file-or-domain> [more...]

Args may mix existing file paths (CSV or plain-text domain lists) and bare
domain / URL tokens. Files are ingested first, then inline domains; a single
combined seed pass follows. UUID-shaped args are not seedable (enrich-only) and
are reported under `skipped`.

Emits ONE JSON object on stdout (stderr carries progress only):

    {
      "created":   ["<uuid>", ...],         # rows created this run
      "existing":  ["<apex>", ...],          # same-name re-seed (duplicate_key)
      "skipped":   [{"input","resolved","reason"}, ...],
      "collapsed": [{"resolved":"<apex>","owner_name":"<owner>",
                     "incoming_names":[...]}, ...],  # name-divergent merges
      "stale":        [{"id","domain","name"}, ...],  # ALL profile IS NULL
      "seeded_stale": [{"id","domain","name"}, ...],  # stale subset touched this run
      "dry_run":   false,
      "ok": true
    }

The `stale` array is every `profile IS NULL` row in the DB -- the enrich set for
a file or bare invocation (global stale pass). The `seeded_stale` array is the
subset of `stale` whose rows were created or matched this run -- the scoped
enrich set for a domain/URL-token invocation, so a single seeded domain does not
drag the whole stale backlog into enrichment (the Pipeline table's "stale scoped
to those rows"). Each array is a ready-to-consume `args` value for the
lead-companies-enrich Workflow. On a dry run no rows are created, so `stale`
reflects the pre-existing stale set only and `created` is replaced by
`would_create`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

DOMAIN_COLUMN_CANDIDATES = ["domain", "website", "company_url", "url"]
NAME_COLUMN_CANDIDATES = ["company_name", "name", "company"]
CSV_HEADER_TOKENS = DOMAIN_COLUMN_CANDIDATES
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
RESOLVE_WORKERS = 8


def extract_apex(value: str) -> str:
    """Lowercase host, strip a leading ``www.``; preserve other subdomains."""
    value = value.strip()
    if not value:
        return ""
    candidate = value if "//" in value else "https://" + value
    host = urlsplit(candidate).netloc.lower()
    if not host:
        return ""
    # Drop any userinfo / port that slipped through.
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def resolve_apex(apex: str) -> str:
    """Follow the full redirect chain via curl's ``%{url_effective}``.

    Hop-agnostic and CR-free per SPEC.md V.74. ``url_effective`` is always set,
    so a no-redirect host resolves back to ``apex``.
    """
    if not apex:
        return apex
    try:
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "-o",
                "/dev/null",
                "--max-time",
                "12",
                "-w",
                "%{url_effective}",
                "-A",
                "Mozilla/5.0",
                f"https://{apex}/",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return apex
    final_url = result.stdout.strip()
    resolved = extract_apex(final_url) if final_url else apex
    return resolved or apex


def detect_format(path: str) -> str:
    """Return ``"csv"`` or ``"text"`` from the first non-empty raw line.

    Peeks at raw bytes (not the Read tool's line-numbered output): a line with a
    comma and at least one known header token is CSV, else plain text.
    """
    with open(path, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            lowered = line.lower()
            if "," in line and any(tok in lowered for tok in CSV_HEADER_TOKENS):
                return "csv"
            return "text"
    return "text"


def parse_csv(path: str, override_column: str | None) -> list[tuple[str, str]]:
    """Parse CSV via csv.DictReader (RFC-4180, SPEC.md V.74).

    Returns ``(domain, display_name)`` pairs; display_name may be empty.
    """
    pairs: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        column = override_column or next(
            (c for c in DOMAIN_COLUMN_CANDIDATES if c in columns), None
        )
        if column is None:
            sys.exit(f"no domain column found in {path}; name the column with --column")
        name_column = next((c for c in NAME_COLUMN_CANDIDATES if c in columns), None)
        for row in reader:
            value = (row.get(column) or "").strip()
            if not value:
                continue
            name = (row.get(name_column) or "").strip() if name_column else ""
            pairs.append((value, name))
    return pairs


def parse_text(path: str) -> list[tuple[str, str]]:
    """Parse a plain-text domain list -- one domain/URL per non-comment line.

    Line iteration is admitted here per SPEC.md V.74 (non-CSV). Plain-text rows
    carry no display name.
    """
    pairs: list[tuple[str, str]] = []
    with open(path, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pairs.append((line, ""))
    return pairs


def create_company(
    domain: str, name: str, dry_run: bool
) -> tuple[str, dict[str, object]]:
    """Run ``mailpilot company create``; classify the JSON envelope.

    Returns one of ``("created", {...})``, ``("existing", {...})``,
    ``("would_create", {...})`` (dry run), or ``("error", {...})``.
    stdout carries the JSON envelope (race-safe duplicate -> exit 1 with
    ``{"error":"duplicate_key",...}``); the always-on operator-log line is on
    stderr and is discarded so it cannot corrupt the parse.
    """
    if dry_run:
        return "would_create", {"domain": domain, "name": name}
    proc = subprocess.run(
        [
            "uv",
            "run",
            "mailpilot",
            "company",
            "create",
            "--domain",
            domain,
            "--name",
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = proc.stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "error", {"domain": domain, "reason": raw[:200] or "no output"}
    if payload.get("ok") and isinstance(payload.get("company"), dict):
        return "created", payload["company"]
    if payload.get("error") == "duplicate_key":
        return "existing", {"domain": domain}
    return "error", {"domain": domain, "reason": payload.get("error", "unknown")}


def names_diverge(incoming_name: str, owner_name: str) -> bool:
    """Whether an incoming CSV display name signals an entity merge (V.98).

    A non-empty incoming display name that differs (case- and
    whitespace-insensitive) from the apex owner's name flags a hidden
    distinct-entity merge -> record under ``collapsed``. An empty incoming name
    (plain-text / inline domain) carries no merge signal, and a same-name
    re-seed stays a silent ``existing``.
    """
    incoming = re.sub(r"\s+", " ", incoming_name).strip().casefold()
    if not incoming:
        return False
    owner = re.sub(r"\s+", " ", owner_name).strip().casefold()
    return incoming != owner


def fetch_owner_name(domain: str) -> str:
    """Return the existing company row's name for ``domain`` (SPEC.md V.98).

    On a create ``duplicate_key`` the resolved apex is already owned; comparing
    the owner's name to the incoming CSV display name distinguishes a silent
    re-seed (same name) from a hidden entity merge (divergent name). The domain
    is a natural key (SPEC.md V.90), so ``company view <domain>`` resolves the
    one owning row directly (SPEC.md V.107) -- no fuzzy ``company search`` LIKE
    match plus client-side exact-domain filter. Empty string if the row cannot
    be read (unknown domain -> ``not_found`` envelope, no ``company`` key).
    """
    proc = subprocess.run(
        ["uv", "run", "mailpilot", "company", "view", domain],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        company = json.loads(proc.stdout)["company"]
    except json.JSONDecodeError, KeyError:
        return ""
    return str(company.get("name") or "")


def query_stale() -> list[dict[str, str]]:
    """Capture rows with ``profile IS NULL`` projected for the enrich Workflow."""
    proc = subprocess.run(
        ["uv", "run", "mailpilot", "company", "list", "--no-profile"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        companies = json.loads(proc.stdout)["companies"]
    except json.JSONDecodeError, KeyError:
        return []
    return [
        {"id": c["id"], "domain": c["domain"], "name": c["name"]} for c in companies
    ]


def collect_inputs(
    args: list[str], override_column: str | None
) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
    """Resolve args into ``(domain, name)`` pairs; report unseedable args.

    Files (CSV/text) are ingested first, then inline domain tokens. UUID-shaped
    args are enrich-only and returned under ``skipped``.
    """
    file_args = [a for a in args if os.path.isfile(a)]
    other_args = [a for a in args if not os.path.isfile(a)]
    pairs: list[tuple[str, str]] = []
    skipped: list[dict[str, object]] = []
    for path in file_args:
        if detect_format(path) == "csv":
            pairs.extend(parse_csv(path, override_column))
        else:
            pairs.extend(parse_text(path))
    for token in other_args:
        if UUID_RE.match(token):
            skipped.append(
                {"input": token, "resolved": None, "reason": "uuid_not_seedable"}
            )
            continue
        pairs.append((token, ""))
    return pairs, skipped


def seed(pairs: list[tuple[str, str]], dry_run: bool) -> dict[str, list[object]]:
    """Resolve apexes (concurrently, each unique apex once), then create rows."""
    apexes = [extract_apex(domain) for domain, _ in pairs]
    unique_apexes = sorted({a for a in apexes if a})
    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
        resolved_map = dict(
            zip(unique_apexes, pool.map(resolve_apex, unique_apexes), strict=True)
        )
    resolved_list = [resolved_map.get(a, a) for a in apexes]

    created: list[str] = []
    would_create: list[dict[str, object]] = []
    existing: list[str] = []
    skipped: list[dict[str, object]] = []
    owner_name_by_apex: dict[str, str] = {}
    divergent_names_by_apex: dict[str, list[str]] = defaultdict(list)
    seen_resolved: set[str] = set()

    def record_collision(resolved: str, display_name: str) -> None:
        """Route a resolved-apex collision to collapsed vs silent existing (V.98)."""
        owner_name = owner_name_by_apex.get(resolved, "")
        if names_diverge(display_name, owner_name):
            names = divergent_names_by_apex[resolved]
            incoming = display_name.strip()
            if incoming not in names:
                names.append(incoming)
        else:
            existing.append(resolved)

    for (domain, display_name), apex, resolved in zip(
        pairs, apexes, resolved_list, strict=True
    ):
        if not apex:
            skipped.append({"input": domain, "resolved": None, "reason": "unparseable"})
            continue
        if resolved in seen_resolved:
            # Collapsed onto an apex already handled this run (intra-batch).
            record_collision(resolved, display_name)
            continue
        seen_resolved.add(resolved)
        name = display_name if display_name else resolved
        status, payload = create_company(resolved, name, dry_run)
        if status == "created":
            created.append(str(payload["id"]))
            owner_name_by_apex[resolved] = name
        elif status == "would_create":
            would_create.append(payload)
            owner_name_by_apex[resolved] = name
        elif status == "existing":
            # Previously-seeded apex: the owner's name decides merge vs re-seed.
            owner_name_by_apex[resolved] = fetch_owner_name(resolved)
            record_collision(resolved, display_name)
        else:
            skipped.append(
                {
                    "input": domain,
                    "resolved": resolved,
                    "reason": str(payload.get("reason", "create_failed")),
                }
            )

    collapsed = [
        {
            "resolved": resolved,
            "owner_name": owner_name_by_apex.get(resolved, ""),
            "incoming_names": sorted(names),
        }
        for resolved, names in divergent_names_by_apex.items()
    ]
    return {
        "created": created,
        "would_create": would_create,
        "existing": existing,
        "skipped": skipped,
        "collapsed": collapsed,
        # Every resolved apex created or matched this run -- the scope key for
        # `seeded_stale` (a domain-token run enriches only rows it touched).
        "touched_apexes": sorted(seen_resolved),
    }


def scope_stale_to_seeded(
    stale: list[dict[str, str]],
    created_ids: set[str],
    touched_apexes: set[str],
) -> list[dict[str, str]]:
    """Narrow the global stale set to rows seeded or matched this run.

    A domain/URL-token invocation must enrich only the rows it just touched, not
    the whole `profile IS NULL` backlog (the Pipeline table's "stale scoped to
    those rows"). A row qualifies if it was created this run (`id` in
    ``created_ids``) or its domain resolved to an apex handled this run (`domain`
    in ``touched_apexes`` -- covers the duplicate-key re-seed that produced no
    new id). File and bare invocations ignore this and enrich the full ``stale``.
    """
    return [
        row
        for row in stale
        if row["id"] in created_ids or row["domain"] in touched_apexes
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="file path(s) and/or domain token(s)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve + report without creating rows",
    )
    parser.add_argument(
        "--column",
        default=None,
        help="override the CSV domain column auto-detect",
    )
    parsed = parser.parse_args()

    pairs, arg_skipped = collect_inputs(parsed.inputs, parsed.column)
    print(f"parsed {len(pairs)} seedable row(s)", file=sys.stderr)

    result = seed(pairs, parsed.dry_run)
    result["skipped"] = arg_skipped + result["skipped"]

    stale = query_stale()
    created_ids = {str(i) for i in result.get("created") or []}
    touched_apexes = set(result.pop("touched_apexes", []))
    result["stale"] = stale
    result["seeded_stale"] = scope_stale_to_seeded(stale, created_ids, touched_apexes)
    result["dry_run"] = parsed.dry_run
    result["ok"] = True

    if parsed.dry_run:
        # No rows created; surface the would-be creates, drop the empty key.
        result.pop("created", None)
    else:
        result.pop("would_create", None)

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
