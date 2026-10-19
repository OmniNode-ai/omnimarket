# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capture effect promotes the operator's words through its own append path (OMN-19513).

Bus records in, through the effect handler (the serve process's own seam), ledger rows out. The
assistant reply is taken in first, then the operator prompt that answers it; with a promotion
lane the capture rows are followed by one RULING, OPERATOR-CONSENT or inbox MSG. The model and
the ledger are injected ports; every file lives under pytest's tmp_path.

Controls: with no promotion lane the same records write the capture rows only; a crash between
the capture rows and the promotion row writes no row twice, and the resumed MSG id moves with
its restamped row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.operator_capture import (
    EnumOperatorCaptureStatus,
    ModelOperatorPromptRecord,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_serve import (
    assistant_reply_of,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_operator_capture_effect import (
    HandlerOperatorCaptureEffect,
)

pytestmark = pytest.mark.unit

SESSION = "4fb6b47e-ec68-4a15-ab71-0de49ccbcec6"
LANE = "orchestrator-bb1a"
REPLY = (
    "The Claude voters now run beside the two lab voters.\n\n"
    "Keep the Claude reviewers only as a fallback path behind the lab voters, or make them the "
    "main reviewers?"
)
WORDS = "I said I do not want them as a fallback path. They should be the main fucking reviewers."
ANSWER = json.dumps(
    {
        "items": [
            {
                "kind": "decision",
                "quote": "They should be the main fucking reviewers.",
                "subject": "main reviewers",
                "confidence": 0.9,
            }
        ]
    }
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
    def __init__(self, answer: str | None) -> None:
        self.answer = answer

    def __call__(self, prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
        if self.answer is None:
            return DelegateAnswer(None, None, "onex delegate exit 1: no consumer")
        return DelegateAnswer(self.answer, "Qwen3.8-27B", None)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 3, 26, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _handler(
    tmp_path: Path,
    ledger: FakeLedger,
    *,
    answer: str | None = ANSWER,
    lane: str | None = LANE,
    clock: Clock | None = None,
) -> HandlerOperatorCaptureEffect:
    ledger_path = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger_path.touch()
    return HandlerOperatorCaptureEffect(
        store_dir=tmp_path / "store",
        ledger_path=ledger_path,
        host_name="ledger-host",
        delegate=FakeDelegate(answer),
        append=ledger,
        clock=clock or Clock(),
        promotion_lane=lane,
    )


def _reply() -> ModelOperatorPromptRecord:
    return ModelOperatorPromptRecord(
        session_id=SESSION,
        content_kind="assistant_reply",
        content=REPLY,
        emitted_at="2026-10-18T03:24:10Z",
    )


def _prompt() -> ModelOperatorPromptRecord:
    return ModelOperatorPromptRecord(
        session_id=SESSION,
        content_kind="operator_prompt",
        content=WORDS,
        said_at="2026-10-18T03:24:53Z",
        prompt_id="p-1",
    )


def test_reply_then_prompt_appends_a_ruling_after_the_capture_rows(
    tmp_path: Path,
) -> None:
    ledger = FakeLedger()
    handler = _handler(tmp_path, ledger)
    noted = handler.handle(_reply())
    assert noted.status is EnumOperatorCaptureStatus.REPLY_NOTED
    assert ledger.rows == []
    receipt = handler.handle(_prompt())
    assert receipt.status is EnumOperatorCaptureStatus.RECORDED
    assert [r.split(" | ")[1] for r in ledger.rows] == ["STATUS", "RULING"]
    ruling = ledger.rows[1]
    assert f"| lane={LANE} |" in ruling
    assert (
        "| question=Keep the Claude reviewers only as a fallback path behind the lab voters, or "
        "make them the main reviewers? |"
    ) in ruling
    assert f"| session={SESSION} |" in ruling
    assert ruling.endswith('"They should be the main fucking reviewers."')


def test_heuristic_words_go_to_the_orchestrator_inbox(tmp_path: Path) -> None:
    ledger = FakeLedger()
    handler = _handler(tmp_path, ledger, answer=None)
    handler.handle(_reply())
    handler.handle(_prompt())
    msgs = [r for r in ledger.rows if r.split(" | ")[1] == "MSG"]
    assert len(msgs) == 1
    assert f"| to={LANE} |" in msgs[0]
    assert "They should be the main fucking reviewers." in msgs[0]


def test_control_no_promotion_lane_writes_capture_rows_only(tmp_path: Path) -> None:
    ledger = FakeLedger()
    handler = _handler(tmp_path, ledger, lane=None)
    handler.handle(_reply())
    handler.handle(_prompt())
    assert [r.split(" | ")[1] for r in ledger.rows] == ["STATUS"]


def test_control_no_reply_goes_to_the_inbox(tmp_path: Path) -> None:
    ledger = FakeLedger()
    handler = _handler(tmp_path, ledger)
    handler.handle(_prompt())
    assert [r.split(" | ")[1] for r in ledger.rows] == ["STATUS", "MSG"]
    assert "no joined question" in ledger.rows[1]


def test_reply_after_the_prompt_is_not_its_question(tmp_path: Path) -> None:
    ledger = FakeLedger()
    handler = _handler(tmp_path, ledger)
    late = _reply().model_copy(update={"emitted_at": "2026-10-18T03:25:30Z"})
    handler.handle(late)
    handler.handle(_prompt())
    assert ledger.rows[-1].split(" | ")[1] == "MSG"


def test_crash_before_the_promotion_row_resumes_without_doubles(tmp_path: Path) -> None:
    clock = Clock()
    failing = FakeLedger(fail_after=1)
    handler = _handler(tmp_path, failing, answer=None, clock=clock)
    handler.handle(_reply())
    receipt = handler.handle(_prompt())
    assert receipt.status is EnumOperatorCaptureStatus.PENDING
    assert [r.split(" | ")[1] for r in failing.rows] == ["STATUS"]
    failing.fail_after = None
    clock.now = datetime(2026, 10, 18, 3, 40, 0, tzinfo=UTC)
    handler.retry()
    # The heuristic splits the words into two items: two capture rows, then the one message.
    assert [r.split(" | ")[1] for r in failing.rows] == ["STATUS", "STATUS", "MSG"]
    items = [
        c for r in failing.rows[:2] for c in r.split(" | ") if c.startswith("item=")
    ]
    assert len(set(items)) == 2
    msg = failing.rows[2]
    assert msg.startswith("2026-10-18T03:40:00Z | MSG |")
    assert "| id=2026-10-18T03:40:00Z-operator-promotion |" in msg


def test_assistant_reply_of_takes_the_last_chunk_only() -> None:
    def wire(chunk_index: int, chunk_count: int) -> bytes:
        return json.dumps(
            {
                "session_id": SESSION,
                "content_kind": "assistant_reply",
                "content": REPLY,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
            }
        ).encode()

    assert assistant_reply_of(wire(1, 2)) is not None
    assert assistant_reply_of(wire(0, 2)) is None
    assert assistant_reply_of(json.dumps({"session_id": SESSION}).encode()) is None
