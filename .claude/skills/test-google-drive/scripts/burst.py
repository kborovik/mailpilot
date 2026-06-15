#!/usr/bin/env python3
"""Deterministic burst driver for the test-google-drive oracle (SPEC §V.59).

Owns the burst's setup-side mechanics -- payload generation, the P=N concurrent
fire, the self-heal resend of a dropped outbound row, and the round-trip poll --
so the skill body stays a single call plus JSON gates instead of ~70 lines of
inline bash. Structural-health verdict (the C4 Logfire gates) is NOT this
script's job; it only produces and confirms the burst, then reports what landed.

This is a deterministic CLI driver, NOT a `.claude/workflows/*.js` agent
workflow: there is zero subagent fan-out, and the dev variant's stateful
background `mailpilot run` loop cannot be owned by an ephemeral workflow
subagent. Sends and `email list` shell out to `uv run mailpilot` so the burst
exercises the same CLI path under test (and keeps `workflow_id == null`
operator-outbound semantics); `GmailClient` is imported only for the gmail-mode
poll (SPEC §V.37).

Output: one JSON object on stdout --
  {subjects, qa_ids, t_send_c, t_send_c_epoch, mode, n, persisted, resent,
   round_trip, ok, fatal}
Exit 0 when the payload is well-formed and all N rows persisted (round-trip
shortfall is sanity-only, never fatal); exit 1 with `fatal` set on a subject
collision, a wrong classifier mix, or a genuine send failure (< N persisted
after self-heal) -- all setup artifacts, not the system under test.

`--mode {local|gmail}` absorbs the prod/dev poll divergence so Phases 1-4 of the
skill body are byte-shared across variants:
  local -- dev variant; the local `mailpilot run` loop syncs the reply into the
           outbound account's inbox, matched by the `[TGD-...]` subject bracket.
  gmail -- prod variant; no local run loop, so poll Gmail directly via
           service-account impersonation of the source account.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

QA_PY = Path(__file__).parent / "qa.py"

# Round-trip poll ceiling. The public SLA is 90s for one reply; at N=4/P=4 the
# burst is a single wave, so ~240s is a generous ceiling that keeps the sanity
# poll from false-failing. The poll never derives latency (SPEC §V.61 -- that is
# the C4 span query); it only confirms replies round-trip.
POLL_ATTEMPTS = 48
POLL_INTERVAL_SECONDS = 5

MAILPILOT = ["uv", "run", "mailpilot"]


def _run_json(args: list[str]) -> dict:
    """Run a `mailpilot` subcommand and parse its JSON stdout envelope."""
    completed = subprocess.run(
        [*MAILPILOT, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"mailpilot {' '.join(args)} exited {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def _qa_pick(args: list[str]) -> dict:
    """Run `qa.py pick` and parse its JSON pair."""
    completed = subprocess.run(
        ["uv", "run", "python", str(QA_PY), "pick", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"qa.py pick {' '.join(args)} exited {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def _random_topic() -> str:
    """Two short dictionary words, else a base32 token. ASCII only.

    The topic is generated here, deterministically per process, so the skill
    never has the LLM invent or copy a subject across runs (LLMs anchor on
    examples and collide Logfire windows). Falls back to urandom when
    `/usr/share/dict/words` is unavailable.
    """
    import secrets

    words_path = Path("/usr/share/dict/words")
    if words_path.exists():
        candidates = [
            word
            for word in words_path.read_text(encoding="utf-8", errors="replace").split()
            if re.fullmatch(r"[A-Za-z]{4,9}", word)
        ]
        if len(candidates) >= 2:
            return " ".join(secrets.choice(candidates) for _ in range(2))
    token = secrets.token_hex(8)
    return token[:10]


def _make_subjects(count: int) -> list[str]:
    """`count` distinct `[TGD-<HHMMSS>-<i>] <topic>` subjects (SPEC §V.59)."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%H%M%S")
    return [f"[TGD-{stamp}-{index}] {_random_topic()}" for index in range(1, count + 1)]


def _mix_types(count: int) -> list[str]:
    """Classifier-branch mix exercising all three branches (SPEC §V.57).

    N=4 canonical = 2 in-scope / 1 out-of-scope / 1 compare. For any other N,
    keep one out-of-scope + one compare and fill the rest in-scope so the
    three-branch coverage survives.
    """
    if count < 2:
        return ["inscope"] * count
    return (["inscope"] * (count - 2)) + ["outscope", "compare"]


def _build_payload(count: int) -> tuple[list[str], list[str], list[str]]:
    """Return (subjects, qa_ids, questions); raise on collision / wrong mix."""
    subjects = _make_subjects(count)
    if len(set(subjects)) != count:
        raise ValueError("subject collision in burst")

    qa_ids: list[str] = []
    questions: list[str] = []
    for qa_type in _mix_types(count):
        pair = _qa_pick(["--type", qa_type])
        qa_ids.append(pair["id"])
        questions.append(pair["question"])

    if any(not question for question in questions):
        raise ValueError("empty question in burst payload")
    return subjects, qa_ids, questions


def _fire(account_id: str, target: str, subjects: list[str], bodies: list[str]) -> None:
    """Fire all sends concurrently (P=N) and wait for every process."""
    processes = [
        subprocess.Popen(
            [
                *MAILPILOT,
                "email",
                "send",
                "--account-id",
                account_id,
                "--to",
                target,
                "--subject",
                subject,
                "--body",
                body,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for subject, body in zip(subjects, bodies, strict=True)
    ]
    for process in processes:
        process.wait()


def _persisted_subjects(account_id: str, since_iso: str) -> set[str]:
    """Subjects of outbound rows since the anchor (SPEC §V.4 envelope)."""
    envelope = _run_json(
        [
            "email",
            "list",
            "--account-id",
            account_id,
            "--direction",
            "outbound",
            "--since",
            since_iso,
        ]
    )
    return {row["subject"] for row in envelope["emails"]}


def _self_heal(
    account_id: str,
    target: str,
    subjects: list[str],
    bodies: list[str],
    since_iso: str,
) -> list[str]:
    """Resend any subject that did not persist; return the resent subjects.

    Four concurrent `email send` processes occasionally race the DB/Gmail layer:
    a process exits 0 yet its row never persists, so the fire returns with only
    N-1 rows. That is a SETUP artifact, not the system under test -- the burst
    the C4 oracle measures is the INBOUND drain concurrency, not the outbound
    sends. Resend promptly (do NOT space sends with a sleep -- that erodes the
    inbound burst's overlap, SPEC §V.23) so the resent subject's delivery clock
    stays anchored near the send anchor. One pass suffices.
    """
    landed = _persisted_subjects(account_id, since_iso)
    resent: list[str] = []
    for subject, body in zip(subjects, bodies, strict=True):
        if subject in landed:
            continue
        resent.append(subject)
        subprocess.run(
            [
                *MAILPILOT,
                "email",
                "send",
                "--account-id",
                account_id,
                "--to",
                target,
                "--subject",
                subject,
                "--body",
                body,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return resent


def _poll_local(account_id: str, subjects: list[str], since_iso: str) -> int:
    """Count replies that round-tripped into the local inbox (dev variant).

    The local `mailpilot run` loop syncs the reply into the outbound account's
    inbox; match on the `[TGD-<HHMMSS>-<i>]` bracket (Gmail prepends `Re:`).
    """
    brackets = [subject.split("]")[0] + "]" for subject in subjects]
    for _ in range(POLL_ATTEMPTS):
        envelope = _run_json(
            [
                "email",
                "list",
                "--account-id",
                account_id,
                "--direction",
                "inbound",
                "--since",
                since_iso,
            ]
        )
        inbound_subjects = [row["subject"] for row in envelope["emails"]]
        matched = sum(
            1
            for bracket in brackets
            if any(bracket in subject for subject in inbound_subjects)
        )
        if matched >= len(subjects):
            return matched
        time.sleep(POLL_INTERVAL_SECONDS)
    return matched


def _poll_gmail(source_email: str, target: str, since_epoch: int, count: int) -> int:
    """Count replies via Gmail impersonation of the source (prod variant).

    No local run loop exists in prod; the deployed instance replies from the
    target mailbox. Poll Gmail directly via service-account impersonation of the
    source account (SPEC §V.37).
    """
    from mailpilot.gmail import GmailClient

    client = GmailClient(source_email)
    found = 0
    for _ in range(POLL_ATTEMPTS):
        stubs = client.list_messages(
            query=f"from:{target} after:{since_epoch}",
            label_ids=["INBOX"],
        )
        found = len(stubs)
        if found >= count:
            return found
        time.sleep(POLL_INTERVAL_SECONDS)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-id", required=True, help="outbound source account id"
    )
    parser.add_argument("--target", required=True, help="burst recipient address")
    parser.add_argument(
        "--env",
        required=True,
        help="deployment_environment label (recorded in output; gates filter on it)",
    )
    parser.add_argument("--mode", required=True, choices=["local", "gmail"])
    parser.add_argument(
        "--source-email",
        default="outbound@lab5.ca",
        help="impersonation subject for the gmail-mode poll (SPEC §V.37)",
    )
    parser.add_argument("--n", type=int, default=4, help="burst size (default 4)")
    args = parser.parse_args()

    result: dict[str, object] = {
        "mode": args.mode,
        "env": args.env,
        "n": args.n,
        "ok": False,
        "fatal": None,
    }

    # Payload generation (B1). Collisions / wrong mix are setup artifacts -> fatal.
    try:
        subjects, qa_ids, questions = _build_payload(args.n)
    except (ValueError, RuntimeError) as error:
        result["fatal"] = str(error)
        print(json.dumps(result, indent=2))
        return 1
    result["subjects"] = subjects
    result["qa_ids"] = qa_ids

    # Single wall-clock anchor (B2): epoch + ISO refer to the same instant. All N
    # sends complete within seconds, so one anchor is precise enough for the
    # per-span latency derivation the C4 gates run.
    anchor = datetime.datetime.now(datetime.UTC)
    t_send_c_epoch = int(anchor.timestamp())
    t_send_c = anchor.isoformat()
    result["t_send_c"] = t_send_c
    result["t_send_c_epoch"] = t_send_c_epoch

    # Fire P=N + self-heal the setup-side send flake (B2).
    try:
        _fire(args.account_id, args.target, subjects, questions)
        resent = _self_heal(args.account_id, args.target, subjects, questions, t_send_c)
        landed = _persisted_subjects(args.account_id, t_send_c)
    except (RuntimeError, KeyError) as error:
        result["fatal"] = f"send/list failure: {error}"
        print(json.dumps(result, indent=2))
        return 1
    result["resent"] = resent
    persisted = sum(1 for subject in subjects if subject in landed)
    result["persisted"] = persisted

    # < N persisted after self-heal = genuine send failure, not the system under
    # test -> fatal; the run is invalidated before the C4 verdict.
    if persisted < args.n:
        result["fatal"] = (
            f"only {persisted}/{args.n} outbound rows persisted after self-heal"
        )
        print(json.dumps(result, indent=2))
        return 1

    # Round-trip poll (B3) -- sanity only, never the latency verdict (SPEC §V.61).
    try:
        if args.mode == "local":
            round_trip = _poll_local(args.account_id, subjects, t_send_c)
        else:
            round_trip = _poll_gmail(
                args.source_email, args.target, t_send_c_epoch, args.n
            )
    except (RuntimeError, KeyError) as error:
        result["fatal"] = f"round-trip poll failure: {error}"
        print(json.dumps(result, indent=2))
        return 1
    result["round_trip"] = round_trip

    result["ok"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
