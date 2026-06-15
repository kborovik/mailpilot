"""Send the test emails for a run from the outbound account.

Each email's body is the QA question; the subject carries the MP-TEST tag. Run-A
sends its single email; Run-B fires all four concurrently so they land in one
sync tick and exercise the agent's parallel task drain. Records a window_start
(captured before sending) that later scripts use as the ``--since`` lower bound.

Usage:
    uv run python scripts/send_emails.py --run-id <id> --run A|B
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from _common import mp, read_json, run_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run", required=True, choices=["A", "B"])
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    outbound_id = manifest["outbound_account_id"]
    inbound_email = manifest["inbound_email"]
    cases = [c for c in manifest["cases"] if c["run"] == args.run]

    window_start = datetime.now().astimezone().isoformat()

    def send(case: dict) -> tuple[str, dict]:
        payload = mp(
            [
                "email",
                "send",
                "--account-id",
                outbound_id,
                "--to",
                inbound_email,
                "--subject",
                case["subject"],
                "--body",
                case["question"],
            ],
            check=False,
        )
        email = payload.get("email", {})
        return case["case_id"], {
            "ok": bool(payload.get("ok")),
            "outbound_email_id": email.get("id"),
            "subject": case["subject"],
            "sent_at": email.get("sent_at") or datetime.now().astimezone().isoformat(),
            "error": payload.get("error"),
        }

    sends: dict[str, dict] = {}
    if args.run == "B":
        with ThreadPoolExecutor(max_workers=max(2, len(cases))) as pool:
            for case_id, info in pool.map(send, cases):
                sends[case_id] = info
    else:
        for case in cases:
            case_id, info = send(case)
            sends[case_id] = info

    result = {"run": args.run, "window_start": window_start, "sends": sends}
    write_json(directory / f"sends_{args.run}.json", result)

    sent_ok = sum(1 for info in sends.values() if info["ok"])
    print(
        json.dumps(
            {
                "run": args.run,
                "sent": sent_ok,
                "of": len(cases),
                "window_start": window_start,
            },
            indent=2,
        )
    )
    return 0 if sent_ok == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
