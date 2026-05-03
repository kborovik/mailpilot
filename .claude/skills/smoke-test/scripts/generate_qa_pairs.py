"""Generate question/answer pairs for the MailPilot Demo KB.

For each markdown file in the demo Drive folder, ask Claude Haiku 4.5 to draft
ONE concrete in-scope question whose answer is grounded in that file. The model
also returns the expected_tokens (model number + 1-2 numeric specs) the agent's
reply must contain. Pairs are written to qa_pairs.json.

Out-of-scope pairs are appended from a hand-curated list (questions about
vendors -- Pentair, Evoqua, Grundfos -- that are not in the KB).

Usage:  python generate_qa_pairs.py
"""

from __future__ import annotations

import io
import json
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
    "You generate one (1) realistic customer question grounded in a vendor "
    "datasheet, plus the verifiable evidence that an agent's correct reply "
    "must contain. Constraints: "
    "(1) The question MUST be answerable strictly from the document body. "
    "(2) Pick a question whose answer references at least one model number "
    "AND at least one numeric specification. "
    "(3) Avoid yes/no or trivia questions; prefer 'what is the X of Y?' or "
    "'which model handles Z?'. "
    "(4) expected_tokens must be 2-4 verbatim substrings (case-sensitive) the "
    "agent's reply MUST contain to be considered correct -- include the model "
    "number(s) AND the numeric figure(s) the question targets. "
    "(5) Output STRICT JSON only, no preamble, matching this schema: "
    '{"question": "...", "expected_tokens": ["...", "..."]}'
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


def main() -> int:
    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    files = fetch_md_files()
    print(f"fetched {len(files)} markdowns from Drive")
    pairs: list[dict[str, object]] = []
    for i, (name, content) in enumerate(sorted(files.items()), 1):
        try:
            qa = draft_qa(client, name, content)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  [{i:>2}] FAIL  {name}  ({exc})")
            continue
        pair = {
            "id": f"qa-in-{i:03d}",
            "type": "inscope",
            "source_file": name,
            "question": qa["question"],
            "expected_tokens": qa["expected_tokens"],
        }
        pairs.append(pair)
        print(f"  [{i:>2}] OK    {name}  -> {qa['expected_tokens']}")
    pairs.extend(OUT_OF_SCOPE)
    OUT.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    in_count = sum(1 for p in pairs if p["type"] == "inscope")
    out_count = sum(1 for p in pairs if p["type"] == "outscope")
    print(f"\nwrote {OUT}  ({in_count} in-scope, {out_count} out-of-scope)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
