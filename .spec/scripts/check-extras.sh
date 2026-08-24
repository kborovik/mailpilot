#!/usr/bin/env python3
"""I.nouns / I.verbs set-diff hook for /sdd:check extras-hook.

No-arg: parse SPEC.md §I list-shape vs cli/ registrations. Emit stdout
rows `id|verdict|evidence` (no header). Verdicts: MATCH / MISSING / EXTRA
/ DRIFT. Paths: SPEC_PATH / CLI_PATH env, else cwd SPEC.md +
src/mailpilot/cli (package dir, or a single file fixture). Exit 0 iff
every row is MATCH.

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
    """Headers are `## §V4` not `## §V.4`. Skip ticks that do not tokenize as rg argv."""
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
        for cmd in _rg_cmds_on_line(line):
            recipes.append((section, lineno, cmd))
    return recipes


def _tokens(cmd: str) -> list[str] | None:
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def _first_rg_segment(tokens: list[str]) -> list[str]:
    for sep in ("|", ";", "||", "&&"):
        if sep in tokens:
            return tokens[: tokens.index(sep)]
    return tokens


def _pattern_token(tokens: list[str]) -> str | None:
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"|", ";", "||", "&&"}:
            return None
        if tok == "--":
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.startswith("-"):
            key = tok.split("=", 1)[0]
            if key in {"-e", "--regexp"}:
                if "=" in tok:
                    return tok.split("=", 1)[1]
                return tokens[i + 1] if i + 1 < len(tokens) else None
            if key in FLAG_WITH_ARG and "=" not in tok:
                i += 2
                continue
            i += 1
            continue
        return tok
    return None


def _is_pathish(tok: str) -> bool:
    if tok.startswith("2>") or tok.startswith(">"):
        return True
    return any(c in tok for c in "/*.")


def _is_rg_argv(cmd: str) -> bool:
    tokens = _tokens(cmd)
    if not tokens or tokens[0] != "rg":
        return False
    pattern = _pattern_token(_first_rg_segment(tokens))
    if pattern is None:
        return False
    if f"'{pattern}'" not in cmd and f'"{pattern}"' not in cmd:
        return False
    for tok in _rg_path_operands_from_tokens(_first_rg_segment(tokens)):
        if not _is_pathish(tok):
            return False
    return True


def _rg_cmds_on_line(line: str) -> list[str]:
    cmds: list[str] = []
    i = 0
    while True:
        start = line.find("`rg ", i)
        if start < 0:
            break
        body = start + 1
        found: str | None = None
        close_at = -1
        j = body
        while True:
            end = line.find("`", j)
            if end < 0:
                break
            candidate = line[body:end].strip()
            if _is_rg_argv(candidate):
                found = candidate
                close_at = end
            j = end + 1
        if found is None:
            i = start + 1
            continue
        cmds.append(found)
        i = close_at + 1
    return cmds


def _has_unquoted_pipe(cmd: str) -> bool:
    tokens = _tokens(cmd)
    if tokens is None:
        return "|" in cmd
    return "|" in tokens


def _needs_shell(cmd: str) -> bool:
    if _has_unquoted_pipe(cmd):
        return True
    tokens = _tokens(cmd)
    if tokens is None:
        return True
    for tok in tokens:
        if tok in {";", "||", "&&"} or tok.startswith("2>") or tok.startswith(">"):
            return True
        if ";" in tok:
            return True
    return any(any(c in p for c in "*?[]") for p in _rg_path_operands(cmd))


def _rg_path_operands_from_tokens(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] != "rg":
        return []
    i = 1
    pattern_seen = False
    paths: list[str] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"|", ";", "||", "&&"}:
            break
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


def _rg_path_operands(cmd: str) -> list[str]:
    tokens = _tokens(cmd)
    if not tokens:
        return []
    return _rg_path_operands_from_tokens(_first_rg_segment(tokens))


def _files_from_stdout(stdout: str, cwd: Path) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in stdout.splitlines():
        if not raw:
            continue
        whole = Path(raw) if Path(raw).is_absolute() else cwd / raw
        if raw not in seen and whole.is_file():
            seen.add(raw)
            found.append(raw)
            continue
        if ":" not in raw:
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


def run_rg(cmd: str, cwd: Path) -> tuple[int | str, list[str]]:
    use_shell = _needs_shell(cmd)
    argv = None if use_shell else _tokens(cmd)
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
    except subprocess.TimeoutExpired:
        return "err", ["timeout"]
    except OSError, subprocess.SubprocessError:
        return "err", ["exec"]
    if result.returncode not in (0, 1):
        return "err", [f"exit={result.returncode}"]
    lines = [ln for ln in result.stdout.splitlines() if ln]
    hit_count = len(lines)
    return hit_count, extract_files(cmd, result.stdout, cwd, hit_count)


def _read_cli(path: Path) -> str | None:
    """Read a CLI file fixture or concatenate a cli/ package directory."""
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if path.is_dir():
        parts = [
            child.read_text(encoding="utf-8")
            for child in sorted(path.rglob("*.py"))
            if child.is_file()
        ]
        return "\n".join(parts) if parts else None
    return None


def extras_hook() -> int:
    spec_path = Path(os.environ.get("SPEC_PATH") or _cwd_default("SPEC.md"))
    cli_path = Path(os.environ.get("CLI_PATH") or _cwd_default("src/mailpilot/cli"))

    if not spec_path.is_file():
        emit("I.nouns", "DRIFT", "SPEC.md missing")
        emit("I.verbs", "DRIFT", "SPEC.md missing")
        return 1
    cli_text = _read_cli(cli_path)
    if cli_text is None:
        emit("I.nouns", "DRIFT", "cli missing")
        emit("I.verbs", "DRIFT", "cli missing")
        return 1

    section = _section_i(spec_path.read_text(encoding="utf-8"))
    spec_nouns = parse_spec_nouns(section)
    spec_verbs = parse_spec_verbs(section)
    code_nouns, code_verbs = parse_cli(cli_text)

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
        os.environ.get("CHECK_EXTRAS_PATH") or _cwd_default(".spec/check-extras.md")
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
