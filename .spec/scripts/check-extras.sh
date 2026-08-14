#!/usr/bin/env python3
"""I.nouns / I.verbs set-diff hook for /sdd:check extras-hook.

Parse SPEC.md §I list-shape vs cli.py registrations. Emit stdout rows
`id|verdict|evidence` (no header). Verdicts: MATCH / MISSING / EXTRA / DRIFT.

Paths: SPEC_PATH / CLI_PATH env, else cwd SPEC.md + src/mailpilot/cli.py.
Exit 0 iff every row is MATCH.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

NOUN_LINE = re.compile(r"^\s*-\s+nouns:\s*(.+)$", re.M)
VERB_LINE = re.compile(r"^\s*-\s+verbs:\s*(.+)$", re.M)
TICK = re.compile(r"`([^`]+)`")
GROUP_DECO = re.compile(r'^@main\.group\((?:["\'](\w+)["\'])?\)\s*$')
CMD_DECO = re.compile(r'^@(\w+)\.command\((?:["\'](\w+)["\'])?\)\s*$')
DEF_NAME = re.compile(r"^def (\w+)")
EXCLUDED_GROUPS = frozenset({"show", "config"})


def _cwd_default(rel: str) -> Path:
    return Path.cwd() / rel


def _section_i(text: str) -> str:
    match = re.search(r"^## §I\b.*$", text, re.M)
    if match is None:
        return ""
    start = match.end()
    nxt = re.search(r"^## ", text[start:], re.M)
    return text[start : start + nxt.start()] if nxt else text[start:]


def _lookahead_def(lines: list[str], start: int, window: int = 8) -> str | None:
    end = min(start + window, len(lines))
    for idx in range(start, end):
        found = DEF_NAME.match(lines[idx])
        if found is not None:
            return found.group(1)
    return None


def parse_spec_nouns(section: str) -> set[str] | None:
    match = NOUN_LINE.search(section)
    if match is None:
        return None
    tokens = [tok.strip() for tok in TICK.findall(match.group(1)) if tok.strip()]
    return set(tokens) if tokens else None


def parse_spec_verbs(section: str) -> set[str] | None:
    match = VERB_LINE.search(section)
    if match is None:
        return None
    ticks = TICK.findall(match.group(1))
    if not ticks:
        return None
    tokens = [part.strip() for part in ticks[0].split("|") if part.strip()]
    return set(tokens) if tokens else None


def parse_cli(text: str) -> tuple[set[str], set[str]]:
    lines = text.splitlines()
    groups: list[str] = []
    for idx, line in enumerate(lines):
        deco = GROUP_DECO.match(line)
        if deco is None:
            continue
        name = deco.group(1) or _lookahead_def(lines, idx + 1)
        if name:
            groups.append(name)
    nouns = {name for name in groups if name not in EXCLUDED_GROUPS}

    verbs: set[str] = set()
    for idx, line in enumerate(lines):
        deco = CMD_DECO.match(line)
        if deco is None:
            continue
        owner, explicit = deco.group(1), deco.group(2)
        if owner not in nouns:
            continue
        name = explicit or _lookahead_def(lines, idx + 1)
        if name:
            verbs.add(name)
    return nouns, verbs


def classify(spec: set[str] | None, code: set[str], kind: str) -> tuple[str, str]:
    if spec is None:
        return "DRIFT", f"unparseable spec {kind}"
    missing = sorted(spec - code)
    extra = sorted(code - spec)
    if not missing and not extra:
        return "MATCH", f"equal n={len(spec)}"
    if missing and extra:
        return (
            "DRIFT",
            f"missing={','.join(missing)} extra={','.join(extra)}",
        )
    if missing:
        return "MISSING", f"missing={','.join(missing)}"
    return "EXTRA", f"extra={','.join(extra)}"


def emit(rid: str, verdict: str, evidence: str) -> None:
    print(f"{rid}|{verdict}|{evidence}")


def main() -> int:
    spec_path = Path(os.environ.get("SPEC_PATH") or _cwd_default("SPEC.md"))
    cli_path = Path(
        os.environ.get("CLI_PATH") or _cwd_default("src/mailpilot/cli.py")
    )

    if not spec_path.is_file():
        emit("I.nouns", "DRIFT", "SPEC.md missing")
        emit("I.verbs", "DRIFT", "SPEC.md missing")
        return 1
    if not cli_path.is_file():
        emit("I.nouns", "DRIFT", "cli.py missing")
        emit("I.verbs", "DRIFT", "cli.py missing")
        return 1

    section = _section_i(spec_path.read_text(encoding="utf-8"))
    spec_nouns = parse_spec_nouns(section)
    spec_verbs = parse_spec_verbs(section)
    code_nouns, code_verbs = parse_cli(cli_path.read_text(encoding="utf-8"))

    rows = [
        ("I.nouns", *classify(spec_nouns, code_nouns, "nouns")),
        ("I.verbs", *classify(spec_verbs, code_verbs, "verbs")),
    ]
    dirty = False
    for rid, verdict, evidence in rows:
        emit(rid, verdict, evidence)
        if verdict != "MATCH":
            dirty = True
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
