"""Validate that every QA source file is grounded in the Drive KB folder.

Uses the SAME access path the agent uses -- ``DriveClient(inbound_email)`` with
service-account delegation and ``drive.readonly`` (§V.34-35) -- so this checks
the agent's real view of the folder, not the operator's personal Drive. Reads
the folder id + inbound email from ``preflight.json``.

Writes ``validate_qa.json`` and exits non-zero on discrepancy or Drive error.

Usage:
    uv run python scripts/validate_qa.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import qa_pairs_path, read_json, repo_root, run_dir, write_json


def _expected_source_files() -> set[str]:
    """All non-null .md filenames referenced by QA-Pairs.json."""
    expected: set[str] = set()
    for pair in read_json(qa_pairs_path()):
        if pair.get("source_file"):
            expected.add(pair["source_file"])
        for key in ("source_files", "source_file_alts"):
            for name in pair.get(key, []) or []:
                expected.add(name)
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    preflight = read_json(directory / "preflight.json")
    folder_id = preflight["drive_folder_id"]
    inbound_email = preflight["inbound_email"]

    expected = _expected_source_files()
    result: dict[str, object] = {
        "drive_folder_id": folder_id,
        "expected_count": len(expected),
    }

    try:
        sys.path.insert(0, str(repo_root() / "src"))
        from mailpilot.drive import DriveClient

        actual = {
            entry["name"]
            for entry in DriveClient(inbound_email).list_markdown(folder_id)
        }
    except Exception as exc:
        result["drive_reachable"] = False
        result["verdict"] = "drive_error"
        result["message"] = str(exc)
        write_json(directory / "validate_qa.json", result)
        print(json.dumps(result, indent=2))
        return 1

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    result["drive_reachable"] = True
    result["found_count"] = len(expected & actual)
    result["missing"] = missing
    result["extra"] = extra
    result["verdict"] = "grounded" if not missing else "discrepancy"

    write_json(directory / "validate_qa.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("verdict", "expected_count", "found_count", "missing")
            },
            indent=2,
        )
    )
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
