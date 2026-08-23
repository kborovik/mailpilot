"""I.nouns / I.verbs set-diff hook (.spec/scripts/check-extras.sh).

Mechanical extras-hook: SPEC §I list-shape vs cli.py noun-group @command.
Stdout rows `id|verdict|evidence` (no header). Verdicts MATCH / MISSING /
EXTRA / DRIFT. show/config groups and top-level commands excluded (I.cli).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
HOOK = REPO / ".spec" / "scripts" / "check-extras.sh"

SPEC_MATCH = """\
## §I INTERFACES

- nouns: `account`, `company`.
- verbs: `list|view`.

## §V INVARIANTS
"""

CLI_MATCH = """\
@main.group()
def show() -> None:
    pass

@show.command("queue")
def show_queue() -> None:
    pass

@main.group()
def config() -> None:
    pass

@config.command("get")
def config_get() -> None:
    pass

@main.command()
def status() -> None:
    pass

@main.group()
def account() -> None:
    pass

@account.command("list")
def account_list() -> None:
    pass

@account.command("view")
def account_view() -> None:
    pass

@main.group()
def company() -> None:
    pass

@company.command("list")
def company_list() -> None:
    pass
"""


def _write(tmp_path: Path, spec: str, cli: str) -> tuple[Path, Path]:
    spec_path = tmp_path / "SPEC.md"
    cli_path = tmp_path / "cli.py"
    spec_path.write_text(spec)
    cli_path.write_text(cli)
    return spec_path, cli_path


def run(
    spec: Path | None = None,
    cli: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if spec is not None:
        env["SPEC_PATH"] = str(spec)
    if cli is not None:
        env["CLI_PATH"] = str(cli)
    return subprocess.run(
        [str(HOOK)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def rows(stdout: str) -> dict[str, tuple[str, str]]:
    parsed: dict[str, tuple[str, str]] = {}
    for line in stdout.splitlines():
        assert line.count("|") == 2, line
        assert not line.startswith("id|"), line
        rid, verdict, evidence = line.split("|", 2)
        parsed[rid] = (verdict, evidence)
    return parsed


def test_hook_is_executable() -> None:
    """Extras-hook contract: file exists and is executable."""
    assert HOOK.is_file()
    mode = HOOK.stat().st_mode
    assert mode & stat.S_IXUSR


def test_live_repo_match() -> None:
    """I.nouns + I.verbs: live SPEC.md §I lists match cli.py registrations."""
    result = run()
    parsed = rows(result.stdout)
    assert result.returncode == 0, result.stdout
    assert parsed["I.nouns"][0] == "MATCH"
    assert parsed["I.verbs"][0] == "MATCH"
    assert parsed["I.nouns"][1].startswith("equal n=")
    assert parsed["I.verbs"][1].startswith("equal n=")


def test_fixture_match_excludes_show_config_and_toplevel(
    tmp_path: Path,
) -> None:
    """I.cli: show/config groups and top-level commands are not in the sets."""
    spec_path, cli_path = _write(tmp_path, SPEC_MATCH, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 0, result.stdout
    assert parsed == {
        "I.nouns": ("MATCH", "equal n=2"),
        "I.verbs": ("MATCH", "equal n=2"),
    }


def test_nouns_missing(tmp_path: Path) -> None:
    """I.nouns MISSING: spec noun absent from @main.group()."""
    spec = SPEC_MATCH.replace(
        "`account`, `company`",
        "`account`, `company`, `task`",
    )
    spec_path, cli_path = _write(tmp_path, spec, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.nouns"] == ("MISSING", "missing=task")
    assert parsed["I.verbs"][0] == "MATCH"


def test_nouns_extra(tmp_path: Path) -> None:
    """I.nouns EXTRA: @main.group() noun absent from spec list."""
    cli = CLI_MATCH + "\n@main.group()\ndef tag() -> None:\n    pass\n"
    spec_path, cli_path = _write(tmp_path, SPEC_MATCH, cli)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.nouns"] == ("EXTRA", "extra=tag")


def test_nouns_drift(tmp_path: Path) -> None:
    """I.nouns DRIFT: both missing and extra nonempty."""
    spec = SPEC_MATCH.replace(
        "`account`, `company`",
        "`account`, `task`",
    )
    spec_path, cli_path = _write(tmp_path, spec, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.nouns"] == ("DRIFT", "missing=task extra=company")


def test_verbs_missing(tmp_path: Path) -> None:
    """I.verbs MISSING: spec verb absent from noun-group @command."""
    spec = SPEC_MATCH.replace("`list|view`", "`list|view|send`")
    spec_path, cli_path = _write(tmp_path, spec, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.verbs"] == ("MISSING", "missing=send")


def test_verbs_extra(tmp_path: Path) -> None:
    """I.verbs EXTRA: noun-group @command absent from spec pipe-list."""
    cli = CLI_MATCH + (
        '\n@account.command("create")\ndef account_create() -> None:\n    pass\n'
    )
    spec_path, cli_path = _write(tmp_path, SPEC_MATCH, cli)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.verbs"] == ("EXTRA", "extra=create")


def test_verbs_drift(tmp_path: Path) -> None:
    """I.verbs DRIFT: both missing and extra nonempty."""
    spec = SPEC_MATCH.replace("`list|view`", "`list|send`")
    spec_path, cli_path = _write(tmp_path, spec, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.verbs"] == ("DRIFT", "missing=send extra=view")


def test_unparseable_spec_is_drift(tmp_path: Path) -> None:
    """Unparseable §I lists emit DRIFT, not a silent MATCH."""
    spec_path, cli_path = _write(tmp_path, "## §I INTERFACES\n\nno lists\n", CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 1
    assert parsed["I.nouns"][0] == "DRIFT"
    assert parsed["I.verbs"][0] == "DRIFT"
    assert "unparseable" in parsed["I.nouns"][1]
    assert "unparseable" in parsed["I.verbs"][1]


# --- emit-rg -----------------------------------------------------------------

EXTRAS_FIXTURE = """\
## Recipe grep-runner

- `rg 'alpha' src/a.py`

## §V4 — hits

- `rg 'alpha' src/a.py` -> present
- `rg 'missing' src/a.py` -> zero hits
- `rg 'alpha' src/a.py | grep alpha` -> present

## Other header

- `rg 'alpha' src/a.py`

## §V5 — more

- `rg 'beta' src/a.py` -> present
"""


def run_emit_rg(
    extras: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extras is not None:
        env["CHECK_EXTRAS_PATH"] = str(extras)
    return subprocess.run(
        [str(HOOK), "emit-rg"],
        cwd=cwd or REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def emit_rows(stdout: str) -> list[tuple[str, int, int, str]]:
    parsed: list[tuple[str, int, int, str]] = []
    for line in stdout.splitlines():
        assert not line.startswith("id|"), line
        section, line_no, hit_count, files = line.split("|", 3)
        parsed.append((section, int(line_no), int(hit_count), files))
    return parsed


def _write_emit_fixture(tmp_path: Path) -> Path:
    extras = tmp_path / "check-extras.md"
    extras.write_text(EXTRAS_FIXTURE)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("alpha\nalpha\nbeta\n")
    return extras


def test_emit_rg_parses_backticked_rg_under_v_headers(tmp_path: Path) -> None:
    """Parse: only backticked rg under ## §Vn; skip other headers."""
    extras = _write_emit_fixture(tmp_path)
    result = run_emit_rg(extras, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = emit_rows(result.stdout)
    sections_and_lines = [(section, line) for section, line, _, _ in parsed]
    assert sections_and_lines == [
        ("V4", 7),
        ("V4", 8),
        ("V4", 9),
        ("V5", 17),
    ]


def test_emit_rg_emits_section_line_hit_count_files(tmp_path: Path) -> None:
    """Execute + emit: section|line|hit_count|files for each recipe."""
    extras = _write_emit_fixture(tmp_path)
    result = run_emit_rg(extras, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = emit_rows(result.stdout)
    by_line = {line: row for row in parsed for line in [row[1]]}
    assert by_line[7] == ("V4", 7, 2, "src/a.py")
    assert by_line[8] == ("V4", 8, 0, "")
    assert by_line[9] == ("V4", 9, 2, "src/a.py")
    assert by_line[17] == ("V5", 17, 1, "src/a.py")


def test_emit_rg_zero_hits_are_not_a_verdict(tmp_path: Path) -> None:
    """Prose expectations stay operator-judged: zero hits emit count 0, not FAIL."""
    extras = _write_emit_fixture(tmp_path)
    result = run_emit_rg(extras, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        section, line_no, hit_count, _files = line.split("|", 3)
        assert section.startswith("V")
        assert line_no.isdigit()
        assert hit_count.isdigit()
        assert hit_count not in {"MATCH", "MISSING", "EXTRA", "DRIFT"}
        assert hit_count not in {"HOLD", "VIOLATE", "FAIL"}
    zero = [row for row in emit_rows(result.stdout) if row[2] == 0]
    assert zero == [("V4", 8, 0, "")]


def test_emit_rg_no_arg_path_unchanged(tmp_path: Path) -> None:
    """No-arg extras-hook path stays I.nouns / I.verbs set-diff."""
    spec_path, cli_path = _write(tmp_path, SPEC_MATCH, CLI_MATCH)
    result = run(spec_path, cli_path)
    parsed = rows(result.stdout)
    assert result.returncode == 0, result.stdout
    assert list(parsed) == ["I.nouns", "I.verbs"]
    assert parsed["I.nouns"][0] == "MATCH"
    assert parsed["I.verbs"][0] == "MATCH"


def test_emit_rg_missing_extras_file(tmp_path: Path) -> None:
    """Missing check-extras.md is a hook error, not a silent empty MATCH."""
    missing = tmp_path / "absent.md"
    result = run_emit_rg(missing, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "check-extras.md" in result.stderr


def test_live_emit_rg_well_formed() -> None:
    """Live .spec/check-extras.md rows are section|line|hit_count|files."""
    result = run_emit_rg()
    assert result.returncode == 0, result.stderr
    parsed = emit_rows(result.stdout)
    assert parsed
    sections = {section for section, _, _, _ in parsed}
    assert "V4" in sections
    v4_cli = [
        row
        for row in parsed
        if row[0] == "V4" and "cli.py" in row[3] and row[2] > 0
    ]
    assert v4_cli, result.stdout.splitlines()[:5]
