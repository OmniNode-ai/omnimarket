# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The savings projection takes hook events four at a time, not one (OMN-23043 AC2).

``projection_savings`` skips every hook event but PostCompact in microseconds
(``test_omn23043_savings_hook_event_skip.py``), yet on the dev lane its group on
``onex.evt.omniclaude.hook-event.v1`` kept falling behind: measured 2026-11-09,
the group took about 3 events a second while producers wrote about 6, with the
handler itself costing 0.14 microseconds per skipped event. A sibling group on the
same topic, ``projection_claude_hook_events``, declares ``consume_concurrency`` and
kept up (lag 69 against 17,091). The runtime dispatches a serial consumer's records
one at a time through a worker thread, and each waits on the GIL, so a cheap record
still costs a round trip. The contract now declares the same four in flight.

Golden chain: the savings contract declares four records in flight and the
runtime's own loader reads it as non-serial. Error chain: concurrent ignored events
open no bindings and a PostCompact event still reaches the runner exactly once.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from omnimarket.nodes.node_projection_savings.handlers.handler_runtime_writer import (
    SavingsProjectionWriter,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1] / "src/omnimarket/nodes/node_projection_savings"
)
HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
DECLARED_IN_FLIGHT = 4


def test_savings_contract_declares_four_records_in_flight() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["consume_concurrency"] == {
        "max_in_flight_records": DECLARED_IN_FLIGHT
    }


def test_the_runtime_loader_reads_the_savings_bound_as_concurrent() -> None:
    loader = pytest.importorskip(
        "omnibase_infra.runtime.auto_wiring.models.model_consume_concurrency"
    )
    declared = loader.load_consume_concurrency(NODE_DIR / "contract.yaml")
    assert declared.max_in_flight_records == DECLARED_IN_FLIGHT
    assert not declared.is_serial


class _RecordingRunner:
    """Stands in for the runner: counts constructions, which open the bindings."""

    constructed: ClassVar[int] = 0
    seen: ClassVar[list[dict[str, Any]]] = []
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        with type(self)._lock:
            type(self).constructed += 1

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: object
    ) -> bool:
        with type(self)._lock:
            type(self).seen.append(data)
        return True

    async def close_runtime_resources(self) -> None:
        return None


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> SavingsProjectionWriter:
    _RecordingRunner.constructed = 0
    _RecordingRunner.seen = []
    monkeypatch.setattr(SavingsProjectionWriter, "runner_type", _RecordingRunner)
    instance = SavingsProjectionWriter()
    yield instance
    instance.close()


def _request(offset: int, **payload: Any) -> dict[str, Any]:
    return {"_topic": HOOK_TOPIC, "_partition": 0, "_offset": offset, **payload}


def test_concurrent_ignored_events_open_no_bindings(
    writer: SavingsProjectionWriter,
) -> None:
    requests = [
        _request(offset, event_id=f"e-{offset}", hook_event_name="PostToolBatch")
        for offset in range(DECLARED_IN_FLIGHT * 8)
    ]
    with ThreadPoolExecutor(max_workers=DECLARED_IN_FLIGHT) as pool:
        results = list(pool.map(writer.handle, requests))
    assert results == [{"projected": True}] * len(requests)
    assert _RecordingRunner.constructed == 0


def test_post_compact_among_concurrent_ignored_events_reaches_the_runner_once(
    writer: SavingsProjectionWriter,
) -> None:
    requests = [
        _request(offset, event_id=f"e-{offset}", hook_event_name="PreToolUse")
        for offset in range(DECLARED_IN_FLIGHT * 4)
    ]
    requests.insert(
        5, _request(900, event_id="compaction-1", hook_event_name="PostCompact")
    )
    with ThreadPoolExecutor(max_workers=DECLARED_IN_FLIGHT) as pool:
        results = list(pool.map(writer.handle, requests))
    assert all(result == {"projected": True} for result in results)
    assert [seen["event_id"] for seen in _RecordingRunner.seen] == ["compaction-1"]
    assert _RecordingRunner.constructed == 1
