"""Split analysis edits into GitHub issue payloads, one per suggestion.

Reads ``analysis.json`` and writes:

- ``issues_input.json`` -- compact list the orchestrator files against GitHub
- ``issue_bodies/<nn>.md`` -- steno issue body (problem, current/proposed,
  Acceptance)

Routing is deterministic from the edit ``target``:

- ``code:...`` -- this repo (Mailpilot protocol / classifier). ``repo`` is
  null; the orchestrator files via sdd:github ISSUE against cwd.
- ``toml:...`` -- workflow wording. ``repo`` is ``kborovik/lab5.ca``; source
  of truth is ``campaigns/<workflow>/workflows/<workflow>.toml``.

Does not call ``gh``. The orchestrator owns create, dedup, and ``issues.json``.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from _common import read_json, repo_root, run_dir, write_json

LAB5_GITHUB_REPO = "kborovik/lab5.ca"
LAB5_WORKFLOW_PATH = "campaigns/{workflow}/workflows/{workflow}.toml"
MAILPILOT_TEMPLATES = "src/mailpilot/agent/templates.py"
MAILPILOT_CLASSIFY = "src/mailpilot/agent/classify.py"

_TOML_TARGET = re.compile(r"^toml:(.+) (goal|instructions)$")
_TEMPLATES_TARGET = re.compile(r"^code:templates\.py:(.+)$")


def classify_target(target: str) -> dict[str, Any] | None:
    """Return repo routing and the file the Acceptance checklist should name.

    ``repo`` is null for cwd (Mailpilot); set for lab5.ca. Unknown targets
    return None so the orchestrator skips them rather than guessing a repo.
    """
    if target.startswith("toml:"):
        match = _TOML_TARGET.match(target)
        if not match:
            return None
        workflow, field = match.group(1), match.group(2)
        return {
            "repo": LAB5_GITHUB_REPO,
            "path": LAB5_WORKFLOW_PATH.format(workflow=workflow),
            "field": field,
        }
    if target == "code:classify.py":
        return {
            "repo": None,
            "path": MAILPILOT_CLASSIFY,
            "field": "_INSTRUCTIONS",
        }
    match = _TEMPLATES_TARGET.match(target)
    if match:
        return {
            "repo": None,
            "path": MAILPILOT_TEMPLATES,
            "field": match.group(1),
        }
    if target.startswith("code:"):
        return {
            "repo": None,
            "path": target.removeprefix("code:"),
            "field": "wording",
        }
    return None


def issue_title(target: str, summary: str) -> str:
    """Build a GitHub title that is unique per target and lead-first."""
    text = (summary or target).strip().splitlines()[0].strip()
    title = f"Prompt audit ({target}): {text}"
    if len(title) > 256:
        title = title[:253] + "..."
    return title


def _fence(text: str) -> str:
    """Wrap ``text`` in a markdown fence; never nest inside an existing fence."""
    return f"```\n{text.rstrip()}\n```"


def issue_body(
    edit: dict[str, Any], routing: dict[str, Any], run_id: str, index: int
) -> str:
    """Steno issue body: lead with the change, then quoted wording, then Acceptance."""
    target = str(edit.get("target") or "").strip()
    summary = str(edit.get("summary") or "").strip()
    evidence = str(edit.get("evidence") or "").strip()
    path = routing["path"]
    field = routing["field"]
    lead = summary or evidence or f"Apply this prompt-audit edit to `{path}`."
    lines = [
        lead,
        "",
        f"Prompt-audit target: `{target}`",
        "",
    ]
    if evidence and evidence != lead:
        lines.extend([f"Evidence: {evidence}", ""])
    lines.extend(
        [
            "Current wording:",
            "",
            _fence(str(edit.get("current") or "")),
            "",
            "Proposed wording:",
            "",
            _fence(str(edit.get("proposed") or "")),
            "",
            f"Confidence: {edit.get('confidence') or 'unspecified'}.",
            f"Priority: {edit.get('priority', index + 1)}.",
            f"Audit run: `{run_id}`.",
            "",
            "## Acceptance",
            "",
            f"- [ ] Replace the current wording with the proposed wording in `{path}` (`{field}`)",
        ]
    )
    if routing["repo"]:
        lines.append(
            "- [ ] Re-import the workflow into Mailpilot so the live row matches the TOML"
        )
        lines.append(
            "- [ ] Re-run `/mailpilot-campaign-test` or `/mailpilot-reply-test` if the workflow is live"
        )
    else:
        lines.append("- [ ] `make check` passes")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    analysis_path = directory / "analysis.json"
    if not analysis_path.exists():
        write_json(directory / "issues_input.json", {"edits": []})
        print("no analysis.json -- 0 edits")
        return 0

    analysis = read_json(analysis_path)
    raw_edits = analysis.get("edits") or []
    bodies = directory / "issue_bodies"
    bodies.mkdir(parents=True, exist_ok=True)

    prepared: list[dict[str, Any]] = []
    skipped_unknown = 0
    for index, edit in enumerate(raw_edits):
        target = str(edit.get("target") or "").strip()
        routing = classify_target(target)
        if routing is None:
            skipped_unknown += 1
            continue
        summary = str(edit.get("summary") or "").strip()
        title = issue_title(target, summary)
        body_name = f"{index:02d}.md"
        body_path = bodies / body_name
        body_path.write_text(issue_body(edit, routing, args.run_id, index))
        prepared.append(
            {
                "target": target,
                "repo": routing["repo"],
                "title": title,
                "body_path": str(body_path.relative_to(repo_root())),
                "class": "enhancement",
                "path": routing["path"],
                "field": routing["field"],
            }
        )

    write_json(directory / "issues_input.json", {"edits": prepared})
    mailpilot_n = sum(1 for item in prepared if item["repo"] is None)
    lab5_n = sum(1 for item in prepared if item["repo"] == LAB5_GITHUB_REPO)
    print(
        f"{len(prepared)} edits "
        f"({mailpilot_n} mailpilot cwd, {lab5_n} {LAB5_GITHUB_REPO})"
        + (f"; skipped {skipped_unknown} unknown target" if skipped_unknown else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
