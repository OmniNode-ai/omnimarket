# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_work_ledger_render_effect over the in-memory bus
(OMN-20275, M6 deliverable 2).

Row events on several topics reach the consumer out of order, one is never published,
one is malformed and one names a row id that does not match its text. The chain proves
the rendered file, the offsets the consumer may commit, and the terminal events the node
publishes, with no broker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_work_ledger_render_effect.contract_topics import (
    ALL_SUBSCRIBE_TOPICS,
    CONTRACT_PATH,
    TOPIC_BLOCKED,
    TOPIC_MALFORMED,
    TOPIC_RENDERED,
)
from omnimarket.nodes.node_work_ledger_render_effect.handlers.consumer_work_ledger_render import (
    ConsumerWorkLedgerRender,
)
from tests.unit.nodes.node_work_ledger_render_effect.support import (
    FakeSource,
    file_seq_rows,
    make_file,
    make_node,
    row,
)

pytestmark = pytest.mark.unit

_ROWS = {n: row(n) for n in range(1, 5)}
_ACCEPTED = ALL_SUBSCRIBE_TOPICS[0]  # OMN-25630: the one accepted-row topic


def _payload(seq: int) -> bytes:
    r = _ROWS[seq]
    body: dict[str, Any] = {
        "ledger_id": r.ledger_id,
        "ledger_seq": r.ledger_seq,
        "row_id": r.row_id,
        "raw_row": r.raw_row,
        "envelope_noise": "ignored",
    }
    return json.dumps(body).encode()


async def _collect(bus: EventBusInmemory, topic: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def _on(message: Any) -> None:
        seen.append(json.loads(message.value))

    await bus.subscribe(topic, on_message=_on, group_id=f"collector-{topic}")
    return seen


def test_the_contract_subscribes_the_accepted_row_topic_and_the_node_is_unattached() -> (
    None
):
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    assert ALL_SUBSCRIBE_TOPICS == ("onex.evt.omnimarket.work-ledger-row-accepted.v1",)
    assert contract["event_bus"]["publish_topics"] == [TOPIC_RENDERED, TOPIC_BLOCKED]
    assert contract["event_bus"]["dlq_topics"] == [TOPIC_MALFORMED]
    assert TOPIC_RENDERED == "onex.evt.omnimarket.work-ledger-markdown-rendered.v1"
    assert TOPIC_BLOCKED == "onex.evt.omnimarket.work-ledger-markdown-blocked.v1"
    assert TOPIC_MALFORMED == "onex.dlq.omnimarket.work-ledger-markdown-malformed.v1"
    # a shared runtime subscribes to nothing of this node's
    assert "subscribe_topics" not in contract["event_bus"]
    # one node per file, on the host that owns it: never attached by a shared runtime
    assert contract["lifecycle"] == "experimental"
    assert contract["runtime_dispatch"]["external_trigger"] is True
    assert "handler_routing" not in contract


async def test_out_of_order_rows_lost_tail_and_bad_payloads(tmp_path: Path) -> None:
    bus = EventBusInmemory(environment="test", group="markdown-projection")
    await bus.start()
    rendered = await _collect(bus, TOPIC_RENDERED)
    blocked = await _collect(bus, TOPIC_BLOCKED)
    malformed = await _collect(bus, TOPIC_MALFORMED)
    path = make_file(tmp_path)
    source = FakeSource(_ROWS)
    node = make_node(path, source)
    consumer = ConsumerWorkLedgerRender(node, bus)
    await consumer.start()

    # seq 2 first; seq 4 is never published
    await bus.publish(_ACCEPTED, key=b"rolling-work-ledger", value=_payload(2))
    await bus.publish(_ACCEPTED, key=b"rolling-work-ledger", value=_payload(3))
    await bus.publish(_ACCEPTED, key=b"rolling-work-ledger", value=_payload(1))
    await bus.publish(_ACCEPTED, key=b"k", value=b"not json")
    bad = json.loads(_payload(1))
    bad["raw_row"] = "2026-10-14T10:09:00Z | STATUS | lane=x | tampered"
    await bus.publish(_ACCEPTED, key=b"k", value=json.dumps(bad).encode())
    no_seq = json.loads(_payload(1))
    del no_seq["ledger_seq"]
    await bus.publish(_ACCEPTED, key=b"k", value=json.dumps(no_seq).encode())

    assert file_seq_rows(path) == [_ROWS[n].raw_row for n in (1, 2, 3)]
    assert len(malformed) == 3
    assert blocked == []
    # every event up to the rendered prefix is committable; the lost tail is not yet known
    # the rendered prefix and the dead-lettered payloads are all handled: offsets 0 to 5 of the one topic
    assert consumer.committed_offsets[_ACCEPTED] == 5

    # the lag probe reads the head from the ledger of record and finds seq 4
    await consumer.probe()
    assert file_seq_rows(path) == [_ROWS[n].raw_row for n in (1, 2, 3, 4)]
    seqs = [seq for event in rendered for seq in event["rendered_seqs"]]
    assert seqs == [1, 2, 3, 4]
    assert rendered[-1]["repaired_seqs"] == [4]

    # the ledger of record loses reach: a new head 6 with seq 5 absent blocks visibly
    source.head_override = 6
    await consumer.probe()
    assert blocked[-1]["blocked"]["seq"] == 5
    assert blocked[-1]["blocked"]["reason"] == "source_missing"
    assert file_seq_rows(path) == [_ROWS[n].raw_row for n in (1, 2, 3, 4)]
    await bus.close()
    node.close()


async def test_bootstrap_history_without_a_sequence_is_read_and_not_dead_lettered(
    tmp_path: Path,
) -> None:
    """OMN-25630: history before the first sequenced row has nothing to render; it must not count as malformed."""
    bus = EventBusInmemory(environment="test", group="markdown-projection")
    await bus.start()
    malformed = await _collect(bus, TOPIC_MALFORMED)
    path = make_file(tmp_path)
    node = make_node(path, FakeSource(_ROWS))
    consumer = ConsumerWorkLedgerRender(node, bus)
    await consumer.start()
    history = {
        "ledger_id": _ROWS[1].ledger_id,
        "row_id": _ROWS[1].row_id,
        "raw_row": _ROWS[1].raw_row,
        "source": "bootstrap",
        "legacy_ordinal": 1,
    }
    await bus.publish(_ACCEPTED, key=b"k", value=json.dumps(history).encode())
    await bus.publish(_ACCEPTED, key=b"k", value=_payload(1))
    assert malformed == []
    assert file_seq_rows(path) == [_ROWS[1].raw_row]
    assert consumer.committed_offsets[_ACCEPTED] == 1
    await bus.close()
    node.close()


async def test_the_consumer_restart_resumes_from_the_file(tmp_path: Path) -> None:
    bus = EventBusInmemory(environment="test", group="markdown-projection")
    await bus.start()
    path = make_file(tmp_path)
    first = make_node(path, FakeSource(_ROWS))
    consumer = ConsumerWorkLedgerRender(first, bus)
    await consumer.start()
    await bus.publish(_ACCEPTED, key=b"k", value=_payload(1))
    await consumer.stop()
    first.close()  # crash before the commit is applied to the broker

    second = make_node(path, FakeSource(_ROWS))
    again = ConsumerWorkLedgerRender(second, bus)
    await again.start()
    await bus.publish(_ACCEPTED, key=b"k", value=_payload(1))  # redelivery
    await bus.publish(_ACCEPTED, key=b"k", value=_payload(2))
    assert file_seq_rows(path) == [_ROWS[1].raw_row, _ROWS[2].raw_row]
    await bus.close()
    second.close()
