# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Writer identity comes from the same lane transport as the bus connection."""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import omnibase_infra.cli.delegate_lane as delegate_lane
import omnibase_infra.event_bus.event_bus_kafka as event_bus_kafka
import pytest
from omnibase_infra.cli.delegate_lane import ModelDelegateLaneSelection
from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)
from omnibase_infra.event_bus.model_lane_client_transport import (
    ModelLaneClientTransport,
)
from pydantic import SecretStr

from omnimarket.delegated_test_loop import lane_bus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, bound_lane_principal
from omnimarket.handlers.bus_binding.select import positional_opener
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


def _selection() -> ModelDelegateLaneSelection:
    return ModelDelegateLaneSelection(
        lane="dev",
        bootstrap_servers="broker:9092",
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-256",
        declared_in=Path("lane.yaml"),
    )


class _FakeKafka:
    """Stands in for the broker client: the login resolution is the thing under test."""

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def container_without_a_client_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> pytest.MonkeyPatch:
    """The container's shape: a declared SASL lane, no ``~/.onex`` store, a fake broker client."""
    monkeypatch.setattr(delegate_lane, "resolve_lane_target", lambda **_: _selection())
    monkeypatch.setattr(
        event_bus_kafka.EventBusKafka,
        "from_bootstrap",
        staticmethod(lambda _bootstrap: _FakeKafka()),
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("KAFKA_SASL_USERNAME", raising=False)
    monkeypatch.delenv("KAFKA_SASL_PASSWORD", raising=False)
    return monkeypatch


def _open_bus() -> contextlib.AbstractAsyncContextManager[object]:
    """The bus as the append command opens it: through the binding module's positional opener."""
    return positional_opener(lane_bus.open_lab_run_bus)("kafka", "dev", None, None)


@pytest.mark.asyncio
async def test_environment_login_names_the_writer_inside_the_opened_bus(
    container_without_a_client_store: pytest.MonkeyPatch,
) -> None:
    container_without_a_client_store.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    container_without_a_client_store.setenv("KAFKA_SASL_PASSWORD", "test-password")
    async with _open_bus():
        assert bound_lane_principal() == "writer-under-test"
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()


@pytest.mark.asyncio
async def test_environment_without_a_password_is_not_a_login(
    container_without_a_client_store: pytest.MonkeyPatch,
) -> None:
    container_without_a_client_store.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    with pytest.raises(LabRunBusError, match="Half a credential"):
        async with _open_bus():
            pass
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()


@pytest.mark.asyncio
async def test_a_failed_broker_start_leaves_no_login_behind(
    container_without_a_client_store: pytest.MonkeyPatch,
) -> None:
    class Refusing(_FakeKafka):
        async def start(self) -> None:
            raise InfraConnectionError("broker refused")

    container_without_a_client_store.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    container_without_a_client_store.setenv("KAFKA_SASL_PASSWORD", "test-password")
    container_without_a_client_store.setattr(
        event_bus_kafka.EventBusKafka,
        "from_bootstrap",
        staticmethod(lambda _bootstrap: Refusing()),
    )
    with pytest.raises(LabRunBusError, match="broker refused"):
        async with _open_bus():
            pass
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()


def test_a_bound_plaintext_transport_ignores_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "test-password")
    with (
        bind_lane_client_transport(_transport(None)),
        pytest.raises(LabRunBusError, match="this lane resolves no broker login"),
    ):
        bound_lane_principal()


@pytest.mark.asyncio
async def test_environment_login_picks_the_declared_writer_topic(
    container_without_a_client_store: pytest.MonkeyPatch,
) -> None:
    """Seam: container env -> broker login -> the writer's append command topic."""
    container_without_a_client_store.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    container_without_a_client_store.setenv("KAFKA_SASL_PASSWORD", "test-password")
    topics = ledger_bus.load_work_ledger_append_topics()
    async with _open_bus():
        principal = bound_lane_principal()
        assert principal == "writer-under-test"
        assert topics.writer_command_topic(principal) == (
            "onex.cmd.omnimarket.work-ledger-append-from-writer-under-test.v1"
        )


@pytest.mark.asyncio
async def test_refusals_stand_beside_the_environment_login(
    container_without_a_client_store: pytest.MonkeyPatch,
) -> None:
    """One container, one login: the full credential names the writer; half a credential, a refused
    broker start and a bound plaintext transport still name none."""
    container_without_a_client_store.setenv("KAFKA_SASL_USERNAME", "writer-under-test")
    container_without_a_client_store.setenv("KAFKA_SASL_PASSWORD", "test-password")
    async with _open_bus():
        assert bound_lane_principal() == "writer-under-test"

    class Refusing(_FakeKafka):
        async def start(self) -> None:
            raise InfraConnectionError("broker refused")

    container_without_a_client_store.setattr(
        event_bus_kafka.EventBusKafka,
        "from_bootstrap",
        staticmethod(lambda _bootstrap: Refusing()),
    )
    with pytest.raises(LabRunBusError, match="broker refused"):
        async with _open_bus():
            pass
    with pytest.raises(LabRunBusError, match="this lane resolves no broker login"):
        bound_lane_principal()

    container_without_a_client_store.delenv("KAFKA_SASL_PASSWORD")
    with pytest.raises(LabRunBusError, match="Half a credential"):
        async with _open_bus():
            pass

    container_without_a_client_store.setenv("KAFKA_SASL_PASSWORD", "test-password")
    with (
        bind_lane_client_transport(_transport(None)),
        pytest.raises(LabRunBusError, match="this lane resolves no broker login"),
    ):
        bound_lane_principal()
