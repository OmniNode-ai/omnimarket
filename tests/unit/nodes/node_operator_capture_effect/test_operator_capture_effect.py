# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_operator_capture_effect: inbox to ledger rows, retry, guard, digest and push (OMN-20905..07).

The model and the ledger are injected ports; every file lives under pytest's tmp_path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.operator_capture import (
    EnumGuardVerdict,
    ModelCaptureDigestRequest,
    ModelCaptureGuardRequest,
    ModelCaptureProcessRequest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_cli import (
    main as cli_main,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_guard import (
    HandlerCaptureGuard,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.transcript_read import (
    latest_operator_message,
    operator_messages,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)
TEXT = "There should be no cloud work in M4. Can you check why the h201 floor is stale?"
ANSWER = json.dumps(
    {
        "items": [
            {
                "kind": "decision",
                "quote": "There should be no cloud work in M4.",
                "subject": "cloud work m4",
                "confidence": 0.95,
            },
            {
                "kind": "ask",
                "quote": "Can you check why the h201 floor is stale?",
                "subject": "h201 floor",
                "confidence": 0.9,
            },
        ]
    }
)
RULING = (
    "2026-09-22T20:35:26Z | RULING | lane=m4-board-rescope | ticket=OMN-1 | question=Where does "
    'cloud work go? | kind=process | "No cloud work in M4; it leaves M4 for M4.5."'
)


class FakeLedger:
    def __init__(self, fail_after: int | None = None) -> None:
        self.rows: list[str] = []
        self.fail_after = fail_after

    def __call__(self, row: str) -> tuple[bool, str]:
        if self.fail_after is not None and len(self.rows) >= self.fail_after:
            return False, "ledger append exit 3: locked"
        self.rows.append(row)
        return True, "ok"


class FakeDelegate:
    def __init__(self, answer: str | None = ANSWER) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
        self.calls.append(prompt)
        if self.answer is None:
            return DelegateAnswer(None, None, "onex delegate exit 1: no consumer")
        return DelegateAnswer(self.answer, "Qwen3.8-27B", None)


def _delegate(answer: str | None = ANSWER) -> FakeDelegate:
    return FakeDelegate(answer)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger" / "ROLLING_WORK_LEDGER.md"
    path.parent.mkdir()
    path.write_text(RULING + "\n")
    return path


def _ingest(root: Path, text: str = TEXT, session: str = "sess-1") -> Path | None:
    return capture_store.ingest(
        root,
        session_id=session,
        text=text,
        source="claude-code:remote",
        received_at=NOW,
        origin_event="UserPromptSubmit",
    )


def _process(root: Path, ledger: Path, delegate: Any, append: FakeLedger) -> Any:
    return HandlerCaptureProcess(delegate, append, now=NOW).handle(
        ModelCaptureProcessRequest(store_dir=root, ledger_path=ledger)
    )


def test_a_decision_and_an_ask_become_two_rows_with_words_session_and_drift(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    assert _ingest(root) is not None
    append = FakeLedger()
    result = _process(root, ledger, _delegate(), append)
    assert (result.processed, result.rows_appended, result.pending) == (1, 2, 0)
    decision, ask = append.rows
    assert "kind=decision" in decision
    assert '"There should be no cloud work in M4."' in decision
    assert (
        "drift=re-ruled | relation=reaffirms | prior=2026-09-22T20:35:26Z" in decision
    )
    assert "kind=ask" in ask
    assert "state=open" in ask
    assert "session=sess-1" in ask
    record = json.loads((root / "captures.jsonl").read_text().splitlines()[-1])
    assert record["status"] == "recorded"
    assert record["text"] == TEXT
    assert record["classification"]["classifier"] == "delegated"
    assert oct((root / "captures.jsonl").stat().st_mode & 0o777) == "0o600"


def test_the_same_message_in_the_same_session_is_taken_in_once(tmp_path: Path) -> None:
    root = tmp_path / "store"
    assert _ingest(root) is not None
    assert _ingest(root, TEXT.replace(" ", "  ")) is None
    assert _ingest(root, session="sess-2") is not None


def test_a_machine_injection_yields_no_row_and_no_model_call(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    _ingest(root, "<task-notification>\n<task-id>x</task-id>")
    delegate, append = _delegate(), FakeLedger()
    result = _process(root, ledger, delegate, append)
    assert (result.processed, result.machine, append.rows) == (1, 1, [])
    assert delegate.calls == []


def test_a_model_outage_still_records_through_the_fallback(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    _ingest(root)
    append = FakeLedger()
    result = _process(root, ledger, _delegate(None), append)
    assert result.rows_appended == 2
    assert all("classifier=heuristic" in row for row in append.rows)


def test_a_failed_append_keeps_the_prompt_and_the_retry_writes_no_row_twice(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    _ingest(root)
    append = FakeLedger(fail_after=1)
    first = _process(root, ledger, _delegate(), append)
    assert (first.rows_appended, first.pending) == (1, 1)
    assert first.errors
    assert "locked" in first.errors[0]
    append.fail_after = None
    delegate = _delegate()
    second = _process(root, ledger, delegate, append)
    assert (second.rows_appended, second.pending, second.processed) == (1, 0, 1)
    assert delegate.calls == []
    assert [("kind=decision" in r, "kind=ask" in r) for r in append.rows] == [
        (True, False),
        (False, True),
    ]


def test_a_second_worker_does_not_process_while_one_holds_the_lock(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    _ingest(root)
    with capture_store.worker_lock(root) as held:
        assert held
        result = _process(root, ledger, _delegate(), FakeLedger())
    assert result.processed == 0
    assert result.pending == 1


# -- guard ------------------------------------------------------------------------------------


def _transcript(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def _user(text: Any, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-10-10T17:00:00Z",
        **extra,
    }


def test_transcript_reader_finds_only_operator_messages(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        _user("Build the capture node."),
        _user([{"type": "tool_result", "content": "x"}]),
        _user("scheduled tick", isMeta=True),
        _user("<task-notification>done</task-notification>"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": "Also file the Slack ticket.",
        },
        _user(
            [{"type": "text", "text": "Look at this"}, {"type": "image", "source": {}}]
        ),
    )
    texts = [t for t, _ in operator_messages(path)]
    assert texts == [
        "Build the capture node.",
        "Also file the Slack ticket.",
        "Look at this",
    ]
    assert latest_operator_message(path) == "Look at this"


def _guard(root: Path, transcript: Path, **payload: Any) -> Any:
    body = {
        "tool_name": "Workflow",
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        **payload,
    }
    return HandlerCaptureGuard(now=NOW).handle(
        ModelCaptureGuardRequest(
            store_dir=root, payload=body, env={"CLAUDE_CODE_ENTRYPOINT": "cli"}
        )
    )


def test_guard_refuses_an_uncaptured_message_captures_it_and_the_retry_passes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    transcript = _transcript(tmp_path, _user("Build the capture node."))
    first = _guard(root, transcript)
    assert first.verdict is EnumGuardVerdict.REFUSE
    assert len(capture_store.pending(root)) == 1
    pending = capture_store.read_pending(capture_store.pending(root)[0])
    assert pending["origin_event"] == "dispatch-guard"
    assert pending["source"] == "claude-code:local"
    assert _guard(root, transcript).verdict is EnumGuardVerdict.ALLOW


def test_guard_allows_a_captured_message_a_subagent_and_a_lane(tmp_path: Path) -> None:
    root = tmp_path / "store"
    transcript = _transcript(tmp_path, _user(TEXT))
    _ingest(root)
    assert _guard(root, transcript).verdict is EnumGuardVerdict.ALLOW
    other = _transcript(tmp_path, _user("Something new."))
    assert _guard(root, other, agent_id="a1").verdict is EnumGuardVerdict.ALLOW
    lane = HandlerCaptureGuard(now=NOW).handle(
        ModelCaptureGuardRequest(
            store_dir=root,
            payload={
                "tool_name": "Workflow",
                "session_id": "sess-1",
                "transcript_path": str(other),
            },
            env={"ONEX_LANE_ID": "lane-x", "CLAUDE_CODE_ENTRYPOINT": "cli"},
        )
    )
    assert lane.verdict is EnumGuardVerdict.ALLOW
    assert _guard(root, tmp_path / "missing.jsonl").verdict is EnumGuardVerdict.ALLOW


# -- digest and push --------------------------------------------------------------------------


def test_digest_lists_open_asks_and_pushes_overdue_once_per_day(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    ask = (
        "2026-10-08T10:00:00Z | STATUS | lane=operator-capture | kind=ask | item=cap-0123456789 | "
        'ask=ask-0123456789 | state=open | session=s1 | "Fix the dashboard projection."'
    )
    ledger.write_text(RULING + "\n" + ask + "\n")
    pushes: list[str] = []

    def push(text: str) -> tuple[bool, str]:
        pushes.append(text)
        return True, "sent"

    request = ModelCaptureDigestRequest(
        store_dir=root, ledger_path=ledger, now=NOW, push_overdue=True
    )
    first = HandlerCaptureDigest(push=push).handle(request)
    assert (first.open_asks, first.overdue, first.pushed) == (1, 1, ("ask-0123456789",))
    assert "ask-0123456789 (2d OVERDUE)" in first.digest
    assert pushes[0].startswith("DECISION: 1 of your asks have waited over a day")
    second = HandlerCaptureDigest(push=push).handle(request)
    assert second.pushed == ()
    assert len(pushes) == 1


def test_digest_reports_captures_that_have_not_reached_the_ledger(
    tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "store"
    _ingest(root)
    result = HandlerCaptureDigest().handle(
        ModelCaptureDigestRequest(store_dir=root, ledger_path=ledger, now=NOW)
    )
    assert result.pending_captures == 1
    assert "have not reached the ledger yet" in result.digest


# -- hook entry points --------------------------------------------------------------------------


def _run_cli(monkeypatch: pytest.MonkeyPatch, args: list[str], stdin: str) -> int:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli_main(args)


@pytest.fixture
def hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "store"
    monkeypatch.setenv("ONEX_OPERATOR_CAPTURE_DIR", str(root))
    for key in capture_store.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    spawned: list[Path] = []
    monkeypatch.setattr(
        "omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_cli.spawn_worker",
        spawned.append,
    )
    return root


def test_ingest_hook_takes_an_operator_prompt_and_skips_a_lane(
    hook_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps(
        {
            "session_id": "s9",
            "prompt": "Build it.",
            "hook_event_name": "UserPromptSubmit",
        }
    )
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert _run_cli(monkeypatch, ["ingest"], payload) == 0
    assert len(capture_store.pending(hook_env)) == 1
    monkeypatch.setenv("ONEX_LANE_ID", "lane-1")
    other = json.dumps({"session_id": "s9", "prompt": "Brief text."})
    assert _run_cli(monkeypatch, ["ingest"], other) == 0
    assert len(capture_store.pending(hook_env)) == 1


def test_ingest_hook_never_blocks_on_bad_input(
    hook_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert _run_cli(monkeypatch, ["ingest"], "not json") == 0
    assert _run_cli(monkeypatch, ["guard"], "not json") == 0


def test_guard_hook_exits_2_with_the_reason(
    hook_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    transcript = _transcript(tmp_path, _user("Dispatch the lab lane."))
    payload = json.dumps(
        {
            "tool_name": "Workflow",
            "session_id": "s9",
            "transcript_path": str(transcript),
        }
    )
    assert _run_cli(monkeypatch, ["guard"], payload) == 2
    assert "no capture record" in capsys.readouterr().err
    assert _run_cli(monkeypatch, ["guard"], payload) == 0


def test_backfill_takes_every_operator_message_once(
    hook_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _transcript(
        tmp_path,
        _user("First ask."),
        _user("Second ask."),
        _user("<command-name>/x</command-name>"),
    )
    args = [
        "backfill",
        "--transcript",
        str(transcript),
        "--session",
        "s9",
        "--source",
        "claude-code:remote",
    ]
    assert _run_cli(monkeypatch, args, "") == 0
    assert _run_cli(monkeypatch, args, "") == 0
    assert len(capture_store.pending(hook_env)) == 2


@pytest.mark.parametrize(
    ("env", "mode"),
    [
        ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, "local"),
        (
            {
                "CLAUDE_CODE_ENTRYPOINT": "sdk-cli",
                "CLAUDE_CODE_ENVIRONMENT_KIND": "bridge",
            },
            "remote",
        ),
        ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "headless"),
        (
            {
                "CLAUDE_CODE_ENVIRONMENT_KIND": "bridge",
                "CLAUDE_CODE_CHILD_SESSION": "1",
            },
            "headless",
        ),
        ({"CLAUDE_CODE_ENTRYPOINT": "cli", "ONEX_LANE_ID": "x"}, "headless"),
        ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "OMNI_ACCESS_MODE": "local"}, "local"),
    ],
)
def test_session_mode(env: dict[str, str], mode: str) -> None:
    assert capture_store.session_mode(env) == mode
