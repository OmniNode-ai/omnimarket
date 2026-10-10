# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_operator_capture_effect: inbox to ledger rows, retry, digest and push (OMN-20905, OMN-20906).

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
    ModelCaptureDigestRequest,
    ModelCaptureProcessRequest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
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
        source="bus:claude",
        received_at=NOW,
        origin_event="content-captured",
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
