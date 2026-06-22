"""Render and lint the campaign per selected contact -- no network.

For each contact in the manifest, substitutes the subject and body placeholders,
records any personalization gaps (NULL field -> fallback, or unknown token), and
runs the live §V.42 outbound body lint (imported from the app so it stays
byte-identical to what the send path enforces). Writes ``personalized.json`` and
a human-readable preview file per contact, then prints a summary. Run this
before ``send_campaign.py`` so the operator can eyeball the previews and catch a
malformed body before any mail goes out.

Usage:
    uv run python scripts/personalize.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import (
    make_subject,
    personalize,
    read_json,
    repo_root,
    run_dir,
    write_json,
)


def _load_format_lint():
    """Import the app's §V.42 spec-table lint (single source of truth)."""
    sys.path.insert(0, str(repo_root() / "src"))
    from mailpilot.agent.tools import (
        _check_spec_table,  # pyright: ignore[reportPrivateUsage]
    )

    return _check_spec_table


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    check_spec_table = _load_format_lint()

    rendered = []
    for contact in manifest["contacts"]:
        subject_body, subject_gaps = personalize(manifest["subject_template"], contact)
        body, body_gaps = personalize(manifest["body_template"], contact)
        tagged_subject = make_subject(args.run_id, contact["sequence"], subject_body)
        lint_error = check_spec_table(body)
        gaps = subject_gaps + body_gaps
        entry = {
            "sequence": contact["sequence"],
            "contact_id": contact["id"],
            "contact_email": contact["email"],
            "alias": contact["alias"],
            "subject": tagged_subject,
            "body": body,
            "gaps": gaps,
            "lint": "fail" if lint_error else "pass",
            "lint_message": lint_error["message"] if lint_error else None,
        }
        rendered.append(entry)

        preview = directory / f"preview_{contact['sequence']:02d}.md"
        preview.write_text(
            f"# {tagged_subject}\n\n"
            f"_contact: {contact['email']} -> alias: {contact['alias']} | "
            f"gaps: {gaps or 'none'} | lint: {entry['lint']}_\n\n{body}\n"
        )

    write_json(directory / "personalized.json", {"rendered": rendered})

    lint_failures = [e["sequence"] for e in rendered if e["lint"] == "fail"]
    with_gaps = [e["sequence"] for e in rendered if e["gaps"]]
    print(
        json.dumps(
            {
                "rendered": len(rendered),
                "lint_failures": lint_failures,
                "contacts_with_gaps": with_gaps,
            },
            indent=2,
        )
    )
    return 0 if not lint_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
