"""Generate stress-test question/answer pairs for the MailPilot Demo KB.

For each markdown file in the demo Drive folder, ask Claude Haiku 4.5 to draft
ONE COMPLEX in-scope question that stresses the retrieval-grounded sales agent.
The model returns either a stress-grade pair (>=3 distinct specs, exact model
disambiguation, 4-6 expected_tokens) OR a skip verdict for files that yield
only trivial single-spec lookups. The skip path is load-bearing: it keeps the
manifest sparse and hard, not exhaustive and easy. Pairs are written to
qa_pairs.json.

After drafting, a cross-source identifier-collision pass populates
`source_file_alts` on any inscope pair whose model-shape `expected_tokens`
also appear verbatim in another file's body (per §V.57(+) / §B.40 -- e.g.
model `WS36-600-2` lives in two pure-aqua datasheets with divergent specs).
This makes the operator-judged `cites_source_file` gate honour the live KB
shape instead of regrading collisions by hand each run.

Out-of-scope pairs are appended from a hand-curated list (questions about
vendors -- Pentair, Evoqua, Grundfos -- that are not in the KB). Compare pairs
(multi-source synthesis) are hand-curated directly in qa_pairs.json.

Usage:  python generate_qa_pairs.py
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from anthropic import Anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from mailpilot.settings import get_settings

FOLDER_ID = "1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat"
DRIVE_ID = "0AJIvyECg210LUk9PVA"
SUBJECT = "inbound@lab5.ca"
MODEL = "claude-haiku-4-5-20251001"

OUT = Path(__file__).parent / "qa_pairs.json"

SYSTEM = (
    "You generate ONE complex customer question that stress-tests a "
    "retrieval-grounded sales agent over a vendor datasheet. The question "
    "MUST be answerable strictly from the document body, AND it MUST be "
    "hard enough that the agent's reply requires careful reading of "
    "MULTIPLE rows or sections -- not a single-spec lookup. If the file "
    "describes a single product with only one or two trivial specs (e.g. "
    "a one-paragraph case study, a single-model brochure with no spec "
    "table), return a skip verdict instead of forcing a weak question.\n\n"
    "Constraints for an accepted question:\n"
    "(1) The question MUST require pulling >=3 distinct specifications "
    "from the document (mix flow rate, pressure, temperature, capacity, "
    "model number, exchange capacity, operating limit, motor rating, "
    "etc -- across different rows of the spec table).\n"
    "(2) When the document lists multiple model variants in one table, "
    "the question MUST pin a SPECIFIC model number so the agent has to "
    "find the exact row (not the first or last). When the document "
    "describes one product, the question MUST cover >=3 operating "
    "parameters together (e.g. 'flow at 60Hz AND pressure at full load "
    "AND temperature range').\n"
    "(3) Prefer multi-clause questions: 'For model X, what is A, B, and "
    "C?' or 'At condition Y, which model handles A and what is its "
    "rated B and C?'. Avoid yes/no, trivia, or 'name one feature'.\n"
    "(4) expected_tokens MUST be 4-6 verbatim substrings (case-sensitive) "
    "the agent's reply MUST contain: include the EXACT model "
    "number(s) AND every numeric figure (with units) the question "
    "targets. Each token must be specific enough that a wrong-row reply "
    "fails (e.g. '12,650 GPD' not just '12,650').\n"
    "(5) Output STRICT JSON only, no preamble, matching ONE of these "
    "two schemas:\n"
    '  ACCEPT: {"question": "...", "expected_tokens": ["...", "...", "...", "..."]}\n'
    '  SKIP:   {"skip": true, "reason": "..."}'
)

OUT_OF_SCOPE = [
    {
        "id": "qa-out-001",
        "type": "outscope",
        "source_file": None,
        "question": (
            "Which Pentair commercial reverse osmosis system would you "
            "recommend for a 300 GPM brewery feedwater application?"
        ),
        "forbidden_token_pairs": [["Pentair", r"\d"]],
        "decline_signals": ["do not", "not in", "outside", "unable", "do not carry"],
    },
    {
        "id": "qa-out-002",
        "type": "outscope",
        "source_file": None,
        "question": (
            "What's the membrane life expectancy on the Evoqua W3T380000 "
            "industrial RO at 20% recovery?"
        ),
        "forbidden_token_pairs": [["Evoqua", r"\d"]],
        "decline_signals": ["do not", "not in", "outside", "unable", "do not carry"],
    },
    {
        "id": "qa-out-003",
        "type": "outscope",
        "source_file": None,
        "question": (
            "Can you spec a Grundfos CRN dosing pump for chlorine injection at 25 GPH?"
        ),
        "forbidden_token_pairs": [["Grundfos", r"\d"]],
        "decline_signals": ["do not", "not in", "outside", "unable", "do not carry"],
    },
    {
        "id": "qa-out-004",
        "type": "outscope",
        "source_file": None,
        "question": (
            "What's the per-pass salt rejection on a Suez ZeeWeed 500D "
            "ultrafiltration module?"
        ),
        "forbidden_token_pairs": [["Suez", r"\d"], ["ZeeWeed", r"\d"]],
        "decline_signals": ["do not", "not in", "outside", "unable", "do not carry"],
    },
    {
        "id": "qa-out-005",
        "type": "outscope",
        "source_file": None,
        "question": (
            "Which Veolia OPUS II system handles produced water with 50,000 "
            "ppm TDS for oil and gas?"
        ),
        "forbidden_token_pairs": [["Veolia", r"\d"], ["OPUS", r"\d"]],
        "decline_signals": ["do not", "not in", "outside", "unable", "do not carry"],
    },
]


def fetch_md_files() -> dict[str, str]:
    settings = get_settings()
    creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    ).with_subject(SUBJECT)
    svc = build("drive", "v3", credentials=creds)
    listing = (
        svc.files()
        .list(
            q=f"'{FOLDER_ID}' in parents and trashed=false and mimeType='text/markdown'",
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="drive",
            driveId=DRIVE_ID,
            pageSize=200,
        )
        .execute()
    )
    out: dict[str, str] = {}
    for f in listing.get("files", []):
        request = svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        out[f["name"]] = buf.getvalue().decode("utf-8")
    return out


def draft_qa(client: Anthropic, name: str, content: str) -> dict[str, object]:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Source file: {name}\n\n---\n\n{content}",
                    },
                ],
            }
        ],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    return json.loads(text)


_MODEL_TOKEN_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*[-/.][A-Za-z0-9.\-/]*\d[A-Za-z0-9.\-/]*$"
)


def annotate_collisions(
    pairs: list[dict[str, object]], file_contents: dict[str, str]
) -> None:
    """Populate `source_file_alts` on inscope pairs whose model-shape tokens
    also appear in other files per §V.57(+) / §B.40."""

    for pair in pairs:
        if pair.get("type") != "inscope":
            continue
        tokens = pair.get("expected_tokens") or []
        if not isinstance(tokens, list):
            continue
        model_tokens = [
            t for t in tokens if isinstance(t, str) and _MODEL_TOKEN_RE.fullmatch(t)
        ]
        if not model_tokens:
            continue
        primary = pair.get("source_file")
        if not isinstance(primary, str) or not primary:
            continue
        colliders: set[str] = set()
        for fname, body in file_contents.items():
            if fname == primary:
                continue
            if any(token in body for token in model_tokens):
                colliders.add(fname)
        if not colliders:
            continue
        pair["source_file_alts"] = [primary, *sorted(colliders)]


def main() -> int:
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    files = fetch_md_files()
    print(f"fetched {len(files)} markdowns from Drive")
    pairs: list[dict[str, object]] = []
    kept = 0
    for i, (name, content) in enumerate(sorted(files.items()), 1):
        try:
            qa = draft_qa(client, name, content)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  [{i:>2}] FAIL  {name}  ({exc})")
            continue
        if qa.get("skip"):
            print(
                f"  [{i:>2}] SKIP  {name}  ({qa.get('reason', 'no complex question')})"
            )
            continue
        tokens = qa.get("expected_tokens") or []
        if len(tokens) < 4:
            print(
                f"  [{i:>2}] SKIP  {name}  (only {len(tokens)} tokens; stress floor is 4)"
            )
            continue
        kept += 1
        pair = {
            "id": f"qa-in-{kept:03d}",
            "type": "inscope",
            "source_file": name,
            "question": qa["question"],
            "expected_tokens": tokens,
        }
        pairs.append(pair)
        print(f"  [{i:>2}] OK    {name}  -> {tokens}")
    annotate_collisions(pairs, files)
    pairs.extend(OUT_OF_SCOPE)
    OUT.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    in_count = sum(1 for p in pairs if p["type"] == "inscope")
    out_count = sum(1 for p in pairs if p["type"] == "outscope")
    print(f"\nwrote {OUT}  ({in_count} in-scope, {out_count} out-of-scope)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
