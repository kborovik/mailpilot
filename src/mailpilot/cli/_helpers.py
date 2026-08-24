"""Shared CLI batch / reason helpers (not group, output, _db, or resolvers)."""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import json

from mailpilot.cli.main import output, output_error


def _batch_ok(ref: str) -> dict[str, object]:
    """One successful row for a §V.139 stdin batch envelope."""
    return {"ref": ref, "status": "ok"}


def _batch_error(ref: str, code: str, message: str) -> dict[str, object]:
    """One error row for a §V.139 stdin batch envelope."""
    return {"ref": ref, "status": "error", "error": code, "message": message}


def _emit_batch_results(results: list[dict[str, object]]) -> None:
    """Emit the §V.139 results envelope; exit 1 when any row is an error.

    Always writes the full stream to stdout with ``ok: true`` (partial success
    still reports every prior row). Exit 0 iff zero error rows; exit 1 if any
    error, without aborting mid-batch.
    """
    output({"results": results}, record_count=len(results))
    if any(row.get("status") == "error" for row in results):
        raise SystemExit(1)


def _resolve_disable_reason(reason: str | None, reason_file: str | None) -> str:
    r"""Resolve single-entity disable reason from ``--reason`` XOR ``--reason-file``.

    Exactly one source required. File is UTF-8; one trailing newline is stripped
    (``\n`` or ``\r\n``). Empty after resolve → ``validation_error``. Missing
    path → ``not_found``.
    """
    import pathlib

    if reason is not None and reason_file is not None:
        output_error(
            "pass only one of --reason or --reason-file",
            "validation_error",
        )
    if reason is None and reason_file is None:
        output_error(
            "pass --reason or --reason-file",
            "validation_error",
        )
    if reason_file is not None:
        path = pathlib.Path(reason_file)
        if not path.is_file():
            output_error(f"reason file not found: {reason_file}", "not_found")
        text = path.read_text(encoding="utf-8")
        if text.endswith("\r\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
        if text.strip() == "":
            output_error("reason cannot be empty", "validation_error")
        return text
    assert reason is not None
    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    return reason


def _read_stdin_ndjson_lines() -> list[tuple[int, str]]:
    """Read non-empty stdin lines as (1-based line number, stripped text)."""
    import sys

    lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(sys.stdin, start=1):
        stripped = raw.strip()
        if stripped:
            lines.append((line_number, stripped))
    return lines


def _parse_json_object(text: str, *, what: str) -> dict[str, object]:
    """Parse JSON text into an object dict.

    Invalid JSON or a non-object root becomes ``validation_error`` (no DB write).
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(parsed, dict):
        output_error(f"{what} must be a JSON object", "validation_error")
    return parsed


def _parse_future_scheduled_at(value: str | None) -> str | None:
    """Parse ``--scheduled-at`` and reject past or unparseable values."""
    if value is None:
        return None
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        output_error(f"invalid --scheduled-at value: {exc}", "validation_error")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        output_error("--scheduled-at must be in the future", "validation_error")
    return parsed.isoformat()


def _parse_ndjson_object(
    line_number: int, line: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Parse one NDJSON line into an object, or a batch error row.

    Returns ``(payload, None)`` on success, ``(None, error_row)`` on failure.
    """
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _batch_error(
            f"line:{line_number}",
            "validation_error",
            f"invalid JSON: {exc}",
        )
    if not isinstance(parsed, dict):
        return None, _batch_error(
            f"line:{line_number}",
            "validation_error",
            "NDJSON line must be a JSON object",
        )
    return parsed, None


def _required_nonempty_str(
    payload: dict[str, object], key: str, line_number: int
) -> tuple[str | None, str, dict[str, object] | None]:
    """Read a required non-empty string field from an NDJSON object.

    Returns ``(value, ref, None)`` on success or ``(None, ref, error_row)``
    on failure. ``ref`` prefers the field value when present, else ``line:N``.
    """
    raw = payload.get(key)
    ref = (
        str(raw).strip()
        if isinstance(raw, str) and raw.strip()
        else f"line:{line_number}"
    )
    if not isinstance(raw, str) or not raw.strip():
        return None, ref, _batch_error(ref, "validation_error", f"{key} is required")
    return raw.strip(), ref, None


def _optional_str_fields(
    payload: dict[str, object], keys: tuple[str, ...], ref: str
) -> tuple[dict[str, str | None] | None, dict[str, object] | None]:
    """Read optional string fields; first type error becomes a batch error row."""
    values: dict[str, str | None] = {}
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            values[key] = None
            continue
        if not isinstance(raw, str):
            return None, _batch_error(
                ref, "validation_error", f"{key} must be a string"
            )
        values[key] = raw
    return values, None
