# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Self-healing binding episodes stay with code; only overdue outages escalate.

Run on the in-memory bus and the Kafka binding with its producer connection recorded
at the transport boundary. No broker or credentials are used.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from aiokafka import AIOKafkaProducer
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.handlers.bus_binding import outbox, select
from omnimarket.models.bus_binding import EnumBusBinding
from omnimarket.nodes.node_exception_queue_effect.handler import HandlerExceptionQueue
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionQueueRequest,
)
from tests.unit.bus_binding.test_bus_binding_fallback import (
    CONTRACT,
    LANE,
    _LedgerHost,
    _request,
    _Switch,
)
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
)

pytestmark = pytest.mark.unit
START = datetime(2026, 10, 27, 12, tzinfo=UTC)


class _KafkaSwitch(_Switch):
    @contextlib.asynccontextmanager
    async def _open(self, bus: str) -> AsyncIterator[object]:
        if bus != "kafka" or not self.up:
            async with super()._open(bus) as memory:
                yield memory
            return
        self.opened.append(bus)
        kafka = EventBusKafka.from_bootstrap("127.0.0.1:1")
        await kafka.start()
        try:
            yield kafka
        finally:
            await kafka.close()


@pytest.fixture(params=["inmemory", "recorded-kafka"])
def switch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Switch:
    if request.param == "inmemory":
        return _Switch()
    producer = Mock(spec=AIOKafkaProducer)
    producer.start = AsyncMock(spec=AIOKafkaProducer.start)
    producer.stop = AsyncMock(spec=AIOKafkaProducer.stop)
    monkeypatch.setattr(
        "omnibase_infra.event_bus.event_bus_kafka.AIOKafkaProducer",
        Mock(spec=AIOKafkaProducer, return_value=producer),
    )
    return _KafkaSwitch()


def _open(switch: _Switch, workspace: Path, state: Path, at: datetime = START):
    return select.open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        open_bus=switch,
        now=lambda: at,
        announce=lambda _: None,
        pending_outbox=lambda: outbox.pending_count(state),
    )


def _rows(state: Path, audience: str = "orchestrator") -> list[dict]:
    return (
        HandlerExceptionQueue()
        .handle(ModelExceptionQueueRequest(state / "exception-queue", audience))
        .rows
    )


def _history(state: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (state / "exception-queue/exception-queue.jsonl")
        .read_text()
        .splitlines()
    ]


async def test_fallback_is_an_orchestrator_exception_not_a_decision(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path) as bound:
        assert bound.decision.binding is EnumBusBinding.INMEMORY
    assert _rows(tmp_path, "operator") == []
    assert len(_rows(tmp_path)) == 1
    assert _rows(tmp_path)[0]["decision"] is False


async def test_healthy_read_waits_for_replay_then_resolves_same_row(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        outbox.queue_request(tmp_path, _request("queued row"))
    row_id = _rows(tmp_path)[0]["id"]
    switch.up = True
    # A health read alone cannot claim that a nonempty outbox has replayed.
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=5)):
        pass
    assert [row["id"] for row in _rows(tmp_path)] == [row_id]
    host = _LedgerHost()
    async with _open(
        switch, workspace, tmp_path, START + timedelta(seconds=10)
    ) as bound:
        result = await select.replay_outbox(bound, tmp_path, host.append)
        assert result.replayed == 1
        assert _rows(tmp_path) == []
    assert host.rows == ["queued row"]
    resolution = next(
        row for row in _history(tmp_path) if row.get("resolves") == row_id
    )
    assert "durable_health_ok" in resolution["how"]
    assert "2026-10-27T12:00:10" in resolution["how"]
    assert "replayed=1" in resolution["how"]
    assert _rows(tmp_path, "operator") == []


async def test_healthy_empty_outbox_resolves_without_manual_action(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        pass
    switch.up = True
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=1)):
        pass
    assert _rows(tmp_path) == []
    assert "replayed=0" in _history(tmp_path)[-1]["how"]


async def test_only_an_outage_longer_than_contract_bound_escalates_once(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    seconds = yaml.safe_load(CONTRACT.read_text())["recovery_policy"][
        "escalate_after_seconds"
    ]
    async with _open(switch, workspace, tmp_path):
        pass
    for elapsed in (seconds - 1, seconds):
        async with _open(
            switch, workspace, tmp_path, START + timedelta(seconds=elapsed)
        ):
            pass
        assert len(_rows(tmp_path)) == 1
    for elapsed in (seconds + 1, seconds + 2):
        async with _open(
            switch, workspace, tmp_path, START + timedelta(seconds=elapsed)
        ):
            pass
    rows = _rows(tmp_path)
    assert len(rows) == 2
    escalation = next(row for row in rows if row["cls"] == "bus-binding-outage")
    assert f"outage_seconds={seconds + 1}" in escalation["evidence"]
    assert _rows(tmp_path, "operator") == []
    switch.up = True
    async with _open(
        switch, workspace, tmp_path, START + timedelta(seconds=seconds + 3)
    ):
        pass
    assert _rows(tmp_path) == []


async def test_failed_and_partial_replay_keep_exception_until_every_row_replays(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        for n in range(3):
            outbox.queue_request(tmp_path, _request(f"row-{n}"))
    switch.up = True

    async def down(_):
        raise TimeoutError

    host = _LedgerHost()
    async with _open(switch, workspace, tmp_path) as bound:
        result = await select.replay_outbox(bound, tmp_path, down)
        assert result.stopped is not None
    assert len(_rows(tmp_path)) == 1
    async with _open(switch, workspace, tmp_path) as bound:
        await select.replay_outbox(bound, tmp_path, host.append, limit=1)
    assert len(_rows(tmp_path)) == 1
    async with _open(switch, workspace, tmp_path) as bound:
        await select.replay_outbox(bound, tmp_path, host.append)
    assert _rows(tmp_path) == []
    assert "replayed=3" in _history(tmp_path)[-1]["how"]


async def test_new_episode_gets_a_fresh_clock_and_exception(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        pass
    switch.up = True
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=2)):
        pass
    switch.up = False
    async with _open(switch, workspace, tmp_path, START + timedelta(days=1)):
        pass
    assert len(_rows(tmp_path)) == 1
    assert _rows(tmp_path)[0]["cls"] == "bus-binding-degraded"
    assert _rows(tmp_path, "operator") == []


async def test_a_new_failed_read_prevents_an_older_healthy_command_closing_the_row(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        pass
    switch.up = True
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=1)):
        switch.up = False
        async with _open(switch, workspace, tmp_path, START + timedelta(seconds=2)):
            pass
    assert len(_rows(tmp_path)) == 1
    assert not any("resolves" in row for row in _history(tmp_path))


@pytest.mark.parametrize("unreplayed", ["unreadable"])
async def test_unreplayed_outbox_never_produces_a_recovery_receipt(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
    unreplayed: str,
) -> None:
    from omnimarket.models.work_ledger_append import EnumWorkLedgerAppendStatus

    async with _open(switch, workspace, tmp_path):
        queued = outbox.queue_request(tmp_path, _request())
    switch.up = True
    host = _LedgerHost()

    async def refuse(request):
        receipt = await host.append(request)
        return receipt.model_copy(
            update={"status": EnumWorkLedgerAppendStatus.REFUSED, "exit_code": 65}
        )

    if unreplayed == "unreadable":
        queued.write_text("broken request")
    async with _open(switch, workspace, tmp_path) as bound:
        await select.replay_outbox(bound, tmp_path, refuse)
    assert len(_rows(tmp_path)) == 1
    assert not any("resolves" in row for row in _history(tmp_path))


async def test_duplicate_replay_counts_as_delivered_and_does_not_append_again(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    host, request = _LedgerHost(), _request("already landed")
    await host.append(request)
    async with _open(switch, workspace, tmp_path):
        outbox.queue_request(tmp_path, request)
    switch.up = True
    async with _open(switch, workspace, tmp_path) as bound:
        result = await select.replay_outbox(bound, tmp_path, host.append)
        assert result.duplicates == 1
    assert _rows(tmp_path) == []
    assert host.rows == ["already landed"]
    assert "duplicates=1" in _history(tmp_path)[-1]["how"]


def test_escalation_bound_is_loaded_from_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.handlers.bus_binding import recovery

    node = tmp_path / "node_bus_binding_reducer"
    node.mkdir()
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["recovery_policy"]["escalate_after_seconds"] = 37
    (node / "contract.yaml").write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(recovery.resources, "files", lambda _: node)
    assert recovery.escalation_bound_seconds() == 37


async def _refuse_with(host, message: str):
    from omnimarket.models.work_ledger_append import EnumWorkLedgerAppendStatus

    async def refuse(request):
        receipt = await host.append(request)
        return receipt.model_copy(
            update={
                "status": EnumWorkLedgerAppendStatus.REFUSED,
                "exit_code": 65,
                "message": message,
            }
        )

    return refuse


async def test_a_refused_request_closes_the_episode_and_is_reported_as_its_own_case(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """A refusal is final and sits in refused/; it must not hold the binding episode open forever."""
    async with _open(switch, workspace, tmp_path):
        queued = outbox.queue_request(tmp_path, _request("malformed terminal row"))
    request_id = str(outbox.request_id_of(queued))
    switch.up = True
    refuse = await _refuse_with(_LedgerHost(), "ledger_lock: friction guard rejected")
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=10)) as b:
        await select.replay_outbox(b, tmp_path, refuse)
    rows = _rows(tmp_path)
    assert [row["cls"] for row in rows] == ["bus-binding-outbox-refused"]
    assert rows[0]["decision"] is False
    assert request_id in rows[0]["key"]
    assert "friction guard rejected" in rows[0]["evidence"]
    resolution = next(row for row in _history(tmp_path) if "resolves" in row)
    assert "refused=1" in resolution["how"]
    # The next episode starts clean: the old refusal is not reported again.
    switch.up = False
    async with _open(switch, workspace, tmp_path, START + timedelta(minutes=5)):
        pass
    switch.up = True
    async with _open(switch, workspace, tmp_path, START + timedelta(minutes=6)):
        pass
    assert [row["cls"] for row in _rows(tmp_path)] == ["bus-binding-outbox-refused"]


async def test_replay_of_the_recorded_episode_from_2026_10_28_resolves_both_rows(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """The recorded state: episode open since 05:29:46Z, 52 replayed, one refusal filed at 05:29:53Z, rows open."""
    from datetime import UTC, datetime

    from omnimarket.handlers.bus_binding import recovery
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionEmit,
    )
    from omnimarket.nodes.node_exception_queue_effect.models import (
        ModelExceptionEmitRequest,
        ModelExceptionRow,
    )

    started = datetime(2026, 10, 28, 5, 29, 46, tzinfo=UTC)
    async with _open(switch, workspace, tmp_path, started):
        pass
    for cls, evidence in (
        (
            "bus-binding-outage",
            "outage_seconds=1896.95; durable bus health read failed: Kafka",
        ),
    ):
        HandlerExceptionEmit().handle(
            ModelExceptionEmitRequest(
                state_dir=tmp_path / "exception-queue",
                row=ModelExceptionRow(
                    cls=cls,
                    key="lane=dev",
                    source=recovery.SOURCE,
                    evidence=evidence,
                    tried="fell back to the in-memory event bus",
                    proposed_fix="investigate",
                ),
                now="2026-10-28T06:01:23Z",
            )
        )
    record = recovery.read_record(tmp_path, "dev")
    recovery._write_record(
        tmp_path, "dev", {**record, "replayed": 52, "duplicates": 1, "refused": 1}
    )
    refused = outbox.outbox_dir(tmp_path) / outbox.REFUSED_DIRNAME
    refused.mkdir(parents=True)
    ns = int(datetime(2026, 10, 28, 5, 29, 53, tzinfo=UTC).timestamp() * 1e9)
    request = _request("TERMINAL row without a FRICTION row")
    (refused / f"{ns:020d}_{request.request_id}.json").write_text(
        json.dumps(
            {
                **json.loads(request.model_dump_json()),
                "refusal": {"message": "friction guard rejected append"},
            }
        )
    )
    switch.up = True
    async with _open(switch, workspace, tmp_path, started + timedelta(hours=8)):
        pass
    assert [row["cls"] for row in _rows(tmp_path)] == ["bus-binding-outbox-refused"]
    assert not any(
        row["cls"] in ("bus-binding-degraded", "bus-binding-outage")
        for row in _rows(tmp_path)
    )


async def test_a_partial_replay_with_a_refusal_keeps_the_episode_open_until_the_rest_replays(
    switch: _Switch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    async with _open(switch, workspace, tmp_path):
        outbox.queue_request(tmp_path, _request("malformed row"))
        outbox.queue_request(tmp_path, _request("good row"))
    switch.up = True
    refuse = await _refuse_with(_LedgerHost(), "ledger_lock: friction guard rejected")
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=10)) as b:
        await select.replay_outbox(b, tmp_path, refuse, limit=1)
    assert [row["cls"] for row in _rows(tmp_path)] == ["bus-binding-degraded"]
    assert not any("resolves" in row for row in _history(tmp_path))
    async with _open(switch, workspace, tmp_path, START + timedelta(seconds=20)) as b:
        await select.replay_outbox(b, tmp_path, _LedgerHost().append)
    assert [row["cls"] for row in _rows(tmp_path)] == ["bus-binding-outbox-refused"]
    assert any("resolves" in row for row in _history(tmp_path))
