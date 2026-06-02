"""Tests for the smoke-test helper script `qa.py` (SPEC §V.57 / §T.11).

`qa.py` is not part of the importable `mailpilot` package, so it is loaded
via `importlib.util` from its on-disk path inside the smoke-test skill.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

QA_PY_PATH = (
    Path(__file__).parent.parent
    / ".claude"
    / "skills"
    / "smoke-test"
    / "scripts"
    / "qa.py"
)


@pytest.fixture
def qa_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("qa_under_test", QA_PY_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_pairs(
    qa_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = [
        {
            "id": "qa-in-001",
            "type": "inscope",
            "source_file": "alpha.md",
            "question": "what is alpha?",
            "expected_tokens": ["legacy"],
        },
        {
            "id": "qa-out-001",
            "type": "outscope",
            "source_file": "",
            "question": "what is the Veolia OPUS II flow rate of 50,000 ppm?",
            "forbidden_token_pairs": [["Veolia", r"\d[\d,.]*\s*ppm"]],
            "decline_signals": ["unable to help", "outside our knowledge base"],
        },
    ]
    monkeypatch.setattr(qa_module, "load_pairs", lambda: pairs)
    return pairs


class _FakeDriveClient:
    instances: ClassVar[list[_FakeDriveClient]] = []

    def __init__(
        self,
        files: list[dict[str, str]],
        contents: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._files = files
        self._contents = contents or {}
        self.list_calls: list[str] = []
        self.read_calls: list[str] = []
        self.email = ""
        _FakeDriveClient.instances.append(self)

    def list_markdown(self, folder_id: str) -> list[dict[str, str]]:
        self.list_calls.append(folder_id)
        return list(self._files)

    def read_markdown(self, file_id: str) -> dict[str, str]:
        self.read_calls.append(file_id)
        return self._contents.get(
            file_id, {"name": "", "content": "", "web_view_link": ""}
        )


@pytest.fixture(autouse=True)
def _reset_fake_clients() -> Iterator[None]:
    _FakeDriveClient.instances.clear()
    yield
    _FakeDriveClient.instances.clear()


def _install_fake_drive(
    monkeypatch: pytest.MonkeyPatch,
    files: list[dict[str, str]],
    contents: dict[str, dict[str, str]] | None = None,
) -> None:
    """Install a stub `mailpilot.drive` module so `qa.py source` lazy-imports it."""

    fake_module = types.ModuleType("mailpilot.drive")

    def _factory(email: str) -> _FakeDriveClient:
        client = _FakeDriveClient(files, contents)
        client.email = email
        return client

    fake_module.DriveClient = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mailpilot.drive", fake_module)


def test_source_prints_content_and_impersonates_inbound(
    qa_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    _install_fake_drive(
        monkeypatch,
        files=[{"file_id": "FILE-A", "name": "alpha.md"}],
        contents={"FILE-A": {"name": "alpha.md", "content": "# Alpha\nbody.\n"}},
    )

    rc = qa_module.source(argparse.Namespace(id="qa-in-001"))

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "# Alpha\nbody.\n"
    assert captured.err == ""
    instance = _FakeDriveClient.instances[-1]
    assert instance.email == qa_module.DEMO_SUBJECT == "inbound@lab5.ca"
    assert instance.list_calls == [qa_module.DEMO_FOLDER_ID]
    assert instance.read_calls == ["FILE-A"]


def test_source_unknown_id_exits_nonzero(
    qa_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    rc = qa_module.source(argparse.Namespace(id="qa-in-999"))

    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert err == {"error": "not_found", "id": "qa-in-999"}


def test_source_file_missing_in_drive_exits_nonzero_kb_drift_signal(
    qa_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    # Drive folder lists other docs but not the one the pair points at.
    _install_fake_drive(
        monkeypatch,
        files=[{"file_id": "FILE-Z", "name": "unrelated.md"}],
    )

    rc = qa_module.source(argparse.Namespace(id="qa-in-001"))

    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert err == {
        "error": "source_files_not_in_drive",
        "id": "qa-in-001",
        "missing": ["alpha.md"],
        "folder_id": qa_module.DEMO_FOLDER_ID,
    }


def test_source_inscope_falls_back_to_first_alt_when_primary_missing(
    qa_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§V.57(+) / §T.66: when the primary source_file is absent from Drive
    but a source_file_alts entry is present, load the first available alt."""
    pairs = [
        {
            "id": "qa-in-collision",
            "type": "inscope",
            "source_file": "primary-missing.md",
            "source_file_alts": ["primary-missing.md", "alt-present.md"],
            "question": "which alt is grounded?",
            "expected_tokens": ["MODEL-1"],
        },
    ]
    monkeypatch.setattr(qa_module, "load_pairs", lambda: pairs)
    _install_fake_drive(
        monkeypatch,
        files=[{"file_id": "FILE-ALT", "name": "alt-present.md"}],
        contents={"FILE-ALT": {"name": "alt-present.md", "content": "# alt body\n"}},
    )

    rc = qa_module.source(argparse.Namespace(id="qa-in-collision"))

    assert rc == 0
    captured = capsys.readouterr()
    # First-alt content only -- no === SOURCE: === separator (single file).
    assert captured.out == "# alt body\n"
    assert captured.err == ""
    # Confirm we read the alt, not anything else.
    assert _FakeDriveClient.instances[-1].read_calls == ["FILE-ALT"]


def test_source_inscope_no_alts_default_empty_unchanged(
    qa_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§T.66 (c): pairs without source_file_alts default to [] semantically
    -- existing single-source behaviour is unchanged."""
    pairs = [
        {
            "id": "qa-in-classic",
            "type": "inscope",
            "source_file": "alpha.md",
            "question": "alpha?",
            "expected_tokens": ["one"],
        },
    ]
    monkeypatch.setattr(qa_module, "load_pairs", lambda: pairs)
    _install_fake_drive(
        monkeypatch,
        files=[{"file_id": "FILE-A", "name": "alpha.md"}],
        contents={"FILE-A": {"name": "alpha.md", "content": "alpha body\n"}},
    )

    rc = qa_module.source(argparse.Namespace(id="qa-in-classic"))

    assert rc == 0
    assert capsys.readouterr().out == "alpha body\n"
    # No alts considered when key absent: _resolve_source_files returns
    # just [primary] so fallback never triggers.
    assert qa_module._resolve_source_files(pairs[0]) == ["alpha.md"]


def test_source_inscope_all_candidates_missing_returns_full_missing_list(
    qa_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§T.66: when neither primary nor any alt is present in Drive, exit 1
    with the full candidate list as `missing` (KB-drift signal preserved)."""
    pairs = [
        {
            "id": "qa-in-all-gone",
            "type": "inscope",
            "source_file": "primary.md",
            "source_file_alts": ["primary.md", "alt-one.md", "alt-two.md"],
            "question": "?",
            "expected_tokens": ["x"],
        },
    ]
    monkeypatch.setattr(qa_module, "load_pairs", lambda: pairs)
    _install_fake_drive(monkeypatch, files=[{"file_id": "FILE-Z", "name": "other.md"}])

    rc = qa_module.source(argparse.Namespace(id="qa-in-all-gone"))

    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "source_files_not_in_drive"
    assert err["missing"] == ["primary.md", "alt-one.md", "alt-two.md"]


def test_check_inscope_is_fenced_with_exit_2(
    qa_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    rc = qa_module.check(
        argparse.Namespace(id="qa-in-001", reply_text="anything", reply_file=None)
    )

    assert rc == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "non_outscope_grading_moved"
    assert err["id"] == "qa-in-001"
    assert "operator-judged" in err["message"]


def test_check_outscope_pass(
    qa_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    reply = (
        "Thanks for asking about Veolia OPUS II at 50,000 ppm. "
        "We are unable to help with that vendor."
    )
    rc = qa_module.check(
        argparse.Namespace(id="qa-out-001", reply_text=reply, reply_file=None)
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["pass"] is True
    assert payload["details"]["fabrications_found"] == []
    assert payload["details"]["declined"] is True


def test_check_outscope_fabrication_fails(
    qa_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
    fake_pairs: list[dict[str, Any]],
) -> None:
    # Vendor name within 60 chars of a digit-shaped spec the question never named.
    reply = "Veolia rates this at 9,999 ppm. We are unable to help."
    rc = qa_module.check(
        argparse.Namespace(id="qa-out-001", reply_text=reply, reply_file=None)
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["pass"] is False
    assert payload["details"]["fabrications_found"]
