# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: captured operator prompts and ledger rows reach a per-ask state report (OMN-21713).

node_ask_state_effect reads a capture store and a ledger from disk and drives
node_ask_state_compute in process: a chat-only ask is dropped, an ask that spawned a lane is owned,
then done once the lane's TERMINAL lands, a parked ask enters the parked queue. The error leg: a
machine-injected record is not an operator ask and yields nothing, and a missing source is refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_ask_state_compute.models.model_ask_state import (
    EnumAskState,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    AskStateSourceError,
    HandlerAskStateReport,
)
from omnimarket.nodes.node_ask_state_effect.models.model_ask_state_read import (
    ModelAskStateReadRequest,
)

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 10, 31, tzinfo=UTC)
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def _record(n: int, hour: int, quote: str, **extra: object) -> str:
    item = {
        "item_id": f"cap-{n:010x}",
        "kind": "ask",
        "quote": quote,
        "subject": "",
        "confidence": 0.5,
    }
    record: dict[str, object] = {
        "capture_id": f"c{n}",
        "session_id": "s",
        "received_at": f"2026-10-31T{hour:02d}:00:00+00:00",
        "text": quote,
        "status": "recorded",
        "classification": {"origin": "operator", "items": [item]},
    }
    record.update(extra)
    return json.dumps(record)


def _run(tmp_path: Path, records: list[str], rows: list[str]) -> HandlerAskStateReport:
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    (store / "captures.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    (tmp_path / "ROLLING_WORK_LEDGER.md").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )
    return HandlerAskStateReport()


def _request(tmp_path: Path) -> ModelAskStateReadRequest:
    return ModelAskStateReadRequest(
        captures_dir=tmp_path / "store",
        ledger_path=tmp_path / "ROLLING_WORK_LEDGER.md",
        since=SINCE,
        now=NOW,
    )


def test_golden_chain_chat_only_lane_owned_done_and_parked(tmp_path: Path) -> None:
    claim = "2026-10-31T08:05:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | ask=ask-0000000002 | worktree=none | est ~1 lane-hours"
    handler = _run(
        tmp_path,
        [
            _record(1, 7, "Check the MSK topic list"),
            _record(2, 8, "Mint the child ticket"),
            _record(3, 9, "Add little animations"),
        ],
        [
            claim,
            "2026-10-31T09:30:00Z | STATUS | lane=orch | ask=ask-0000000003 | state=parked | reason=after the floor | seq=2",
        ],
    )
    report = handler.handle(_request(tmp_path))
    states = {e.ask_id: e.state for e in report.asks}
    assert states["ask-0000000001"] is EnumAskState.DROPPED
    assert states["ask-0000000002"] is EnumAskState.OWNED
    assert states["ask-0000000003"] is EnumAskState.PARKED
    assert report.dropped == ("ask-0000000001",)
    assert [p.ask_id for p in report.parked_queue] == ["ask-0000000003"]

    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + "2026-10-31T10:00:00Z | TERMINAL | lane=lane-a | friction=none | closes-CLAIM=2026-10-31T08:05:00Z | outcome=done\n",
        encoding="utf-8",
    )
    done = handler.handle(_request(tmp_path))
    assert {e.ask_id: e.state for e in done.asks}["ask-0000000002"] is EnumAskState.DONE


def test_error_leg_machine_record_is_no_ask_and_missing_source_is_refused(
    tmp_path: Path,
) -> None:
    handler = _run(
        tmp_path, [_record(1, 7, "Injected brief text", status="machine")], []
    )
    assert handler.handle(_request(tmp_path)).asks == ()
    (tmp_path / "store" / "captures.jsonl").unlink()
    with pytest.raises(AskStateSourceError):
        handler.handle(_request(tmp_path))
