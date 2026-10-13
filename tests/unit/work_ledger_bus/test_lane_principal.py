# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Writer identity comes from the same lane transport as the bus connection."""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)
from omnibase_infra.event_bus.model_lane_client_transport import (
    ModelLaneClientTransport,
)
from pydantic import SecretStr

from omnimarket.delegated_test_loop import lane_bus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, bound_lane_principal
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers.handler_pr_handoff_ledger_effect import (
    BusLaneLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus import bus as ledger_bus

pytestmark = pytest.mark.unit


def _transport(username: str | None) -> ModelLaneClientTransport:
    return ModelLaneClientTransport(
        lane="dev",
        bootstrap_servers="broker:9092",
        security_protocol="PLAINTEXT" if username is None else "SASL_PLAINTEXT",
        sasl_mechanism=None if username is None else "SCRAM-SHA-256",
        sasl_username=username,
        sasl_password=None if username is None else SecretStr("test-password"),
        declared_in=Path("lane.yaml"),
    )


def test_bound_lane_principal_returns_bound_broker_login() -> None:
    with bind_lane_client_transport(_transport("lane-principal")):
        assert bound_lane_principal() == "lane-principal"
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()


def test_bound_lane_principal_requires_bound_transport() -> None:
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()


@pytest.mark.parametrize("username", ["", None])
def test_bound_lane_principal_requires_broker_login(username: str | None) -> None:
    with (
        bind_lane_client_transport(_transport(username)),
        pytest.raises(LabRunBusError, match="this lane resolves no broker login"),
    ):
        bound_lane_principal()


@pytest.mark.asyncio
@pytest.mark.parametrize("append_fails", [False, True])
async def test_handoff_appender_uses_bound_writer_topic_and_stops_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, append_fails: bool
) -> None:
    opened_args: list[dict[str, object]] = []
    published: list[str] = []
    calls: list[str] = []
    callers: list[ledger_bus.WorkLedgerAppendCaller] = []
    memory = EventBusInmemory(environment="local", group="caller-test")
    original_publish = memory.publish

    async def publish(topic: str, key: bytes | None, value: bytes) -> None:
        published.append(topic)
        if append_fails:
            raise RuntimeError("append publish failed")
        await original_publish(topic, key, value)

    monkeypatch.setattr(memory, "publish", publish)

    @contextlib.asynccontextmanager
    async def open_bus(**kwargs: object) -> AsyncIterator[EventBusInmemory]:
        opened_args.append(kwargs)
        await memory.start()
        host = ledger_bus.WorkLedgerAppendHost(
            memory, Handler(), writer_principals=["lane-principal"]
        )
        await host.start()
        try:
            with bind_lane_client_transport(_transport("lane-principal")):
                yield memory
        finally:
            await host.stop()
            await memory.close()
            calls.append("bus-close")

    class Handler:
        host_name = "ledger"

        def handle(
            self, request: ModelWorkLedgerAppendRequest, *, writer_principal: str | None
        ) -> ModelWorkLedgerAppendReceipt:
            assert writer_principal == "lane-principal"
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=EnumWorkLedgerAppendStatus.ACCEPTED,
                exit_code=0,
                principal=writer_principal,
                message="appended",
                ledger_lines=[1],
                ledger_host=self.host_name,
                duration_ms=0,
            )

    class Caller(ledger_bus.WorkLedgerAppendCaller):
        async def start(self) -> None:
            await super().start()
            if self not in callers:
                callers.append(self)
                calls.append("caller-start")

        async def stop(self) -> None:
            await super().stop()
            calls.append("caller-stop")

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", open_bus)
    monkeypatch.setattr(ledger_bus, "WorkLedgerAppendCaller", Caller)
    request = ModelWorkLedgerAppendRequest(
        request_id=uuid4(),
        rows="exact rows",
        requested_by_lane="request-lane",
        requesting_host="test-host",
        requested_at=datetime.now(UTC),
    )
    appender = BusLaneLedgerAppender("dev", tmp_path)
    if append_fails:
        with pytest.raises(RuntimeError, match="append publish failed"):
            await appender.append(request, timeout_s=2)
    else:
        receipt = await appender.append(request, timeout_s=2)
        assert receipt is not None
        assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
        assert receipt.principal == "lane-principal"
    assert len(opened_args) == 1
    options = opened_args[0]
    assert options["bus"] == "kafka"
    assert options["lane"] == "dev"
    assert options["kafka_bootstrap"] is None
    assert list(options.values()) == ["kafka", "dev", None, tmp_path]
    assert published[0] == callers[0].topics.writer_command_topic("lane-principal")
    assert calls == ["caller-start", "caller-stop", "bus-close"]
    assert callers[0]._unsubscribes == []
    assert callers[0]._pending == {}
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()
