#!/usr/bin/env python3
"""I.nouns / I.verbs set-diff hook for /sdd:check extras-hook.

No-arg: parse SPEC.md §I list-shape vs cli.py registrations. Emit stdout
rows `id|verdict|evidence` (no header). Verdicts: MATCH / MISSING / EXTRA
/ DRIFT. Paths: SPEC_PATH / CLI_PATH env, else cwd SPEC.md +
src/mailpilot/cli.py. Exit 0 iff every row is MATCH.

`emit-rg`: parse every backticked `rg` command under each `## §Vn` in
`.spec/check-extras.md` (CHECK_EXTRAS_PATH or cwd default), run it, emit
`section|line|hit_count|files` (no header). Prose expectations stay
operator-judged. Exit 0 when the file was read; missing file -> 1.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

NOUN_LINE = re.compile(r"^\s*-\s+nouns:\s*(.+)$", re.M)
VERB_LINE = re.compile(r"^\s*-\s+verbs:\s*(.+)$", re.M)
TICK = re.compile(r"`([^`]+)`")
GROUP_DECO = re.compile(r'^@main\.group\((?:["\'](\w+)["\'])?\)\s*$')
CMD_DECO = re.compile(r'^@(\w+)\.command\((?:["\'](\w+)["\'])?\)\s*$')
DEF_NAME = re.compile(r"^def (\w+)")
SECTION_HDR = re.compile(r"^##\s+§(V\d+)\b")
EXCLUDED_GROUPS = frozenset({"show", "config"})
FLAG_WITH_ARG = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-E",
        "-e",
        "-f",
        "-g",
        "-m",
        "-t",
        "-T",
        "--after-context",
        "--before-context",
        "--context",
        "--encoding",
        "--file",
        "--glob",
        "--iglob",
        "--max-count",
        "--max-depth",
        "--max-filesize",
        "--pre",
        "--pre-glob",
        "--regexp",
        "--sort",
        "--sortr",
        "--type",
        "--type-not",
    }
)


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


def parse_rg_recipes(text: str) -> list[tuple[str, int, str]]:
    """Backticked commands starting with `rg ` under each `## §Vn` header."""
    section: str | None = None
    recipes: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hdr = SECTION_HDR.match(line)
        if hdr:
            section = hdr.group(1)
            continue
        if line.startswith("## "):
            section = None
            continue
        if section is None:
            continue
        for tick in TICK.findall(line):
            cmd = tick.strip()
            if cmd.startswith("rg "):
                recipes.append((section, lineno, cmd))
    return recipes


def _tokens(cmd: str) -> list[str] | None:
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def _has_unquoted_pipe(cmd: str) -> bool:
    tokens = _tokens(cmd)
    if tokens is None:
        return "|" in cmd
    return "|" in tokens


def _rg_path_operands(cmd: str) -> list[str]:
    tokens = _tokens(cmd)
    if not tokens:
        return []
    if "|" in tokens:
        tokens = tokens[: tokens.index("|")]
    if not tokens or tokens[0] != "rg":
        return []
    i = 1
    pattern_seen = False
    paths: list[str] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            paths.extend(tokens[i + 1 :])
            break
        if tok.startswith("-"):
            key = tok.split("=", 1)[0]
            if key in FLAG_WITH_ARG and "=" not in tok:
                i += 2
                continue
            i += 1
            continue
        if not pattern_seen:
            pattern_seen = True
            i += 1
            continue
        paths.append(tok)
        i += 1
    return paths


def _files_from_stdout(stdout: str, cwd: Path) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in stdout.splitlines():
        if not raw or ":" not in raw:
            continue
        prefix = raw.split(":", 1)[0]
        if not prefix or prefix in seen:
            continue
        candidate = Path(prefix) if Path(prefix).is_absolute() else cwd / prefix
        if candidate.is_file():
            seen.add(prefix)
            found.append(prefix)
    return found


def extract_files(cmd: str, stdout: str, cwd: Path, hit_count: int) -> list[str]:
    if hit_count == 0:
        return []
    from_out = _files_from_stdout(stdout, cwd)
    if from_out:
        return sorted(from_out)
    files = [p for p in _rg_path_operands(cmd) if (cwd / p).is_file()]
    return sorted(files)


def run_rg(cmd: str, cwd: Path) -> tuple[int, list[str]]:
    argv = None if _has_unquoted_pipe(cmd) else _tokens(cmd)
    try:
        if argv is None:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        else:
            result = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return 0, []
    lines = [ln for ln in result.stdout.splitlines() if ln]
    hit_count = len(lines)
    return hit_count, extract_files(cmd, result.stdout, cwd, hit_count)


def extras_hook() -> int:
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


def emit_rg() -> int:
    extras_path = Path(
        os.environ.get("CHECK_EXTRAS_PATH")
        or _cwd_default(".spec/check-extras.md")
    )
    if not extras_path.is_file():
        print(f"check-extras.md missing: {extras_path}", file=sys.stderr)
        return 1
    cwd = Path.cwd()
    text = extras_path.read_text(encoding="utf-8")
    for section, lineno, cmd in parse_rg_recipes(text):
        hit_count, files = run_rg(cmd, cwd)
        print(f"{section}|{lineno}|{hit_count}|{','.join(files)}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return extras_hook()
    if args == ["emit-rg"]:
        return emit_rg()
    print("usage: check-extras.sh [emit-rg]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
