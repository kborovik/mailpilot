"""Campaign-test preflight enforces outbound@lab5.ca identity (§V.151 / §V.122)."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".grok"
    / "skills"
    / "mailpilot-campaign-test"
    / "scripts"
)


def _load(name: str) -> types.ModuleType:
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Sibling imports inside scripts use bare `from _common import ...`.
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


@pytest.fixture(scope="module")
def preflight() -> types.ModuleType:
    return _load("preflight")


@pytest.fixture(scope="module")
def common() -> types.ModuleType:
    return _load("_common")


def test_required_outbound_signature_fields(common: types.ModuleType) -> None:
    required = common.REQUIRED_OUTBOUND_SIGNATURE
    assert required == {
        "full_name": "Konstantin Borovik",
        "title": "DevOps Engineer",
        "website": "https://lab5.ca",
        "phone": "416-670-0621",
    }
    assert common.REQUIRED_OUTBOUND_DISPLAY_NAME == "Konstantin Borovik"
    # Spelling guard: never the truncated Borovi form.
    assert "Borovi" not in required["full_name"] or required["full_name"].endswith("k")
    assert required["full_name"].endswith("Borovik")


def test_default_workflow_files_are_t1_synthetics(common: types.ModuleType) -> None:
    """Campaign-test default is LLM-T1 then templated-T1 synthetics, not a live campaign."""
    files = common.DEFAULT_WORKFLOW_FILES
    assert len(files) == 2
    assert files[0] == common.DEFAULT_WORKFLOW_FILE
    assert files[0].endswith("workflows/campaign-test-llm-t1.toml")
    assert files[1].endswith("workflows/campaign-test-template-t1.toml")
    for path in files:
        assert Path(path).is_file()
        parsed = tomllib.loads(Path(path).read_text())
        assert parsed["name"] == Path(path).stem
        assert parsed["template"].startswith("outbound")
        assert parsed["touches"] == 1
        assert "calendar.app.google" in parsed["instructions"]
        assert "var-sales-coclose" not in path
        assert "lab5-campaigns" not in path
        assert "ai-engineering" not in path
        assert "acumatica-var-outbound" not in path
    llm = tomllib.loads(Path(files[0]).read_text())
    template = tomllib.loads(Path(files[1]).read_text())
    assert common.t1_mode_from_parsed(llm) == "llm"
    assert common.t1_mode_from_parsed(template) == "template"
    assert not llm.get("touch_copy")
    assert "{{first_name}}" in llm["instructions"]
    copy = template["touch_copy"]
    assert len(copy) == 1
    assert copy[0]["n"] == 1
    assert "{first_name}" in copy[0]["body"]
    assert "{company_name}" in copy[0]["subject"]
    assert "{{" not in copy[0]["body"]
    skill = (
        Path(__file__).resolve().parents[1]
        / ".grok"
        / "skills"
        / "mailpilot-campaign-test"
        / "SKILL.md"
    )
    text = skill.read_text()
    assert "campaign-test-llm-t1.toml" in text
    assert "campaign-test-template-t1.toml" in text


def test_resolve_workflow_records_t1_mode(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    llm: dict[str, object] = {}
    issues: list[str] = []
    preflight._resolve_workflow(common.DEFAULT_WORKFLOW_FILES[0], llm, issues)
    assert llm["t1_mode"] == "llm"
    assert issues == []
    templated: dict[str, object] = {}
    preflight._resolve_workflow(common.DEFAULT_WORKFLOW_FILES[1], templated, issues)
    assert templated["t1_mode"] == "template"
    assert issues == []


def test_t1_path_from_reasoning(common: types.ModuleType) -> None:
    assert common.t1_path_from_reasoning("rendered and sent touch 1") == "rendered"
    assert common.t1_path_from_reasoning("composed and sent touch 1") == "composed"
    assert common.t1_path_from_reasoning("") == "unknown"
    assert common.T1_PATH_REASONING["template"] == "rendered and sent"
    assert common.T1_PATH_REASONING["llm"] == "composed and sent"


def test_check_outbound_signature_missing_blocks(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_signature(
        {"id": "x", "email": "outbound@lab5.ca", "signature": None},
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is False
    assert any("signature missing" in i for i in issues)


def test_check_outbound_signature_mismatch_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    bad = dict(common.REQUIRED_OUTBOUND_SIGNATURE)
    bad["full_name"] = "Wrong"
    preflight._check_outbound_signature(
        {"id": "x", "email": "outbound@lab5.ca", "signature": bad},
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is False
    assert any("signature mismatch" in i for i in issues)


def test_check_outbound_signature_match_ok(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_signature(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "signature": dict(common.REQUIRED_OUTBOUND_SIGNATURE),
        },
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is True
    assert issues == []


def test_check_outbound_display_name_missing_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {"id": "x", "email": "outbound@lab5.ca", "display_name": None},
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is False
    assert any("display_name" in i for i in issues)


def test_check_outbound_display_name_mismatch_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "display_name": "MailPilot Outbound",
        },
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is False
    assert any("display_name" in i for i in issues)
    assert common.REQUIRED_OUTBOUND_DISPLAY_NAME in issues[0]


def test_check_outbound_display_name_match_ok(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "display_name": common.REQUIRED_OUTBOUND_DISPLAY_NAME,
        },
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is True
    assert issues == []
