# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: an operator prompt reaches the ledger, stays open, and closes on evidence.

node_operator_capture_effect drives node_operator_capture_compute in process, as the hooks do:
UserPromptSubmit ingest -> worker (delegated classification, drift, rows appended to a ledger
file) -> session-start digest lists the ask -> a lane's TERMINAL citing the ask with a merged PR
closes it -> the digest is empty. The error leg: a dispatch from a message the prompt hook never
saw is refused, captured, and the retry passes.
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
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_guard import (
    HandlerCaptureGuard,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
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
    capture_store.ingest(
        store,
        session_id="sess-op",
        text=sample["text"],
        source="claude-code:remote",
        received_at=NOW,
        origin_event="UserPromptSubmit",
    )
    result = HandlerCaptureProcess(
        RecordedDelegate(sample), FileLedger(ledger), now=NOW
    ).handle(ModelCaptureProcessRequest(store_dir=store, ledger_path=ledger))
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


def test_golden_chain_error_leg_uncaptured_dispatch_is_refused_then_recorded(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "Also port the ledger roll.",
            }
        )
        + "\n"
    )
    payload: dict[str, Any] = {
        "tool_name": "Workflow",
        "session_id": "sess-op",
        "transcript_path": str(transcript),
    }
    request = ModelCaptureGuardRequest(
        store_dir=store, payload=payload, env={"CLAUDE_CODE_ENVIRONMENT_KIND": "bridge"}
    )
    assert (
        HandlerCaptureGuard(now=NOW).handle(request).verdict is EnumGuardVerdict.REFUSE
    )
    assert len(capture_store.pending(store)) == 1
    assert (
        HandlerCaptureGuard(now=NOW).handle(request).verdict is EnumGuardVerdict.ALLOW
    )
