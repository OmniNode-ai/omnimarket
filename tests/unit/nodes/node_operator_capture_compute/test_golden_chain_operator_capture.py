# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: an operator prompt from the bus reaches the ledger, stays open, closes on evidence.

RULING 2026-10-10T22:40:43Z: no capture hooks. node_operator_capture_effect drives
node_operator_capture_compute in process: an operator_prompt record off the content-capture topic
-> the store's inbox -> worker (delegated classification, drift, rows appended to a ledger file)
-> the digest lists the ask -> a lane's TERMINAL citing the ask with a merged PR closes it -> the
digest is empty. The error leg: a lane brief, a tool record and a later chunk of an operator
prompt are not operator prompts, and yield nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.operator_capture import (
    OPERATOR_PROMPT_KIND,
    EnumOperatorCaptureStatus,
    ModelCaptureDigestRequest,
    ModelOperatorPromptRecord,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_serve import (
    operator_prompt_of,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_operator_capture_effect import (
    HandlerOperatorCaptureEffect,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "week_2026_10_05_samples.json"


def _sample(sample_id: str) -> dict[str, str]:
    samples: list[dict[str, str]] = json.loads(FIXTURE.read_text())
    return next(s for s in samples if s["id"] == sample_id)


class RecordedDelegate:
    """Answers with the model's recorded answer for the message it is given."""

    def __init__(self, sample: dict[str, str]) -> None:
        self.sample = sample

    def __call__(self, prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
        assert prompt.endswith(f"MESSAGE:\n{self.sample['text']}\n")
        return DelegateAnswer(
            self.sample["delegated_response"], self.sample["delegated_model"], None
        )


class FileLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, row: str) -> tuple[bool, str]:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(row + "\n")
        return True, "ok"


def test_golden_chain_prompt_to_ledger_to_open_ask_to_closed(tmp_path: Path) -> None:
    store = tmp_path / "store"
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(
        "2026-09-22T20:35:26Z | RULING | lane=m4-board-rescope | ticket=OMN-1 | question=Cloud "
        'work in M4? | kind=process | "Cloud work leaves M4 for M4.5."\n'
    )
    sample = _sample("m4-scope")
    record = ModelOperatorPromptRecord(
        session_id="sess-op",
        content_kind=OPERATOR_PROMPT_KIND,
        content=sample["text"],
        prompt_id="prompt-1",
        said_at=NOW.isoformat(),
    )
    receipt = HandlerOperatorCaptureEffect(
        store_dir=store,
        ledger_path=ledger,
        host_name="h-ledger",
        delegate=RecordedDelegate(sample),
        append=FileLedger(ledger),
        clock=lambda: NOW,
    ).handle(record)
    result = receipt.result
    assert receipt.status is EnumOperatorCaptureStatus.RECORDED
    assert result.processed == 1
    assert result.pending == 0
    rows = [r for r in ledger.read_text().splitlines() if "lane=operator-capture" in r]
    assert len(rows) == result.rows_appended == 3
    assert all("session=sess-op" in r for r in rows)
    assert all(r.split(" | ")[-1].strip('"') in sample["text"] for r in rows)
    drifted = [r for r in rows if "drift=re-ruled" in r]
    assert drifted
    assert "prior=2026-09-22T20:35:26Z" in drifted[0]

    digest = HandlerCaptureDigest().handle(
        ModelCaptureDigestRequest(store_dir=store, ledger_path=ledger, now=NOW)
    )
    assert digest.open_asks == 1
    ask_row = next(r for r in rows if "kind=ask" in r)
    ask_id = next(c for c in ask_row.split(" | ") if c.startswith("ask=")).removeprefix(
        "ask="
    )
    assert ask_id in digest.digest

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            f"2026-10-10T19:00:00Z | TERMINAL | lane=beta-date | friction=none | closes-ask={ask_id} "
            "| evidence=knowledge-base#88 | beta date set\n"
        )
    closed = HandlerCaptureDigest().handle(
        ModelCaptureDigestRequest(store_dir=store, ledger_path=ledger, now=NOW)
    )
    assert closed.open_asks == 0
    assert closed.digest == "Open operator asks: none."


def _wire(payload: dict[str, Any]) -> bytes:
    return json.dumps({"payload": payload, "event_type": "content.captured"}).encode()


def test_golden_chain_error_leg_a_lane_brief_yields_nothing() -> None:
    brief = {
        "session_id": "sess-lane",
        "content_kind": "prompt",
        "content": "Build lane X. Return under 120 words.",
        "chunk_index": 0,
    }
    tool = {"session_id": "sess-lane", "content_kind": "tool_input", "content": "{}"}
    later_chunk = {
        "session_id": "sess-op",
        "content_kind": OPERATOR_PROMPT_KIND,
        "content": "the rest of a very long message",
        "chunk_index": 1,
        "chunk_count": 2,
    }
    blank = {
        "session_id": "sess-op",
        "content_kind": OPERATOR_PROMPT_KIND,
        "content": " ",
    }
    for payload in (brief, tool, later_chunk, blank):
        assert operator_prompt_of(_wire(payload)) is None
    said = {
        "session_id": "sess-op",
        "content_kind": OPERATOR_PROMPT_KIND,
        "content": "Go.",
    }
    record = operator_prompt_of(_wire(said))
    assert record is not None
    assert record.content == "Go."
