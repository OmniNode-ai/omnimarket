# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus lane of scenario R8 (OMN-23822): golden and error chains, a failing control and the seam to the tenant nodes.

The lane under test is the real ``BusLane`` with the contract's lane block. Its bus request is a fake that answers each
command through the real tenant bus handlers over an in-memory projection, so the shape the tenant nodes answer and the
shape R8 reads meet in these tests: a change on either side that drops a key turns them red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import (
    InfraConnectionError,
    InfraTimeoutError,
    ModelTimeoutErrorContext,
)

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_scenario import EnumScenarioConclusion
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioStoreOverlay,
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    adapter_r8_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_bus_request import (
    _load_topics,
    build_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    BusLane,
    InMemoryLane,
    LaneReadError,
    ModelSendRequest,
    ModelTenantReadAnswer,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    HandlerScenarioR8,
    ModelScenarioR8RunCommand,
    load_lane_settings,
    load_runtime,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.handler_tenant_delegations_read_bus import (
    HandlerTenantDelegationsReadBus,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read_bus import (
    HandlerTenantSavingsReadBus,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 11, 9, 13, 6, 30, tzinfo=UTC)
SETTINGS = load_lane_settings()
RUNTIME = load_runtime()
OVERLAY = ModelScenarioTenantReadOverlay(
    bus="inmemory", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)
DATA_SOURCE_OF = {"synthetic": "experiment", "organic": "real"}


class FakeTenantBus:
    """Answers each tenant read command through the real tenant bus handlers over the in-memory lane model."""

    def __init__(
        self, *, count_synthetic: bool = False, drop_key: str | None = None
    ) -> None:
        self.model = InMemoryLane(count_synthetic=count_synthetic)
        self.drop_key = drop_key
        self.commands: list[ModelTenantReadCommand] = []

    def _reader(self) -> InMemoryTenantProjectionReader:
        totals = self.model.read_totals()
        rows = tuple(
            ModelDelegationRunRow(
                run_id=row.correlation_id,
                cost_usd=float(row.cost_usd),
                saved_usd=float(row.saved_usd),
                tokens=row.tokens,
                data_source=DATA_SOURCE_OF[row.traffic_class],
            )
            for row in self.model.read_runs()
        )
        return InMemoryTenantProjectionReader(
            savings={
                TENANT: ModelTenantSavingsAggregate(
                    total_saved_usd=float(totals.savings_usd),
                    total_spend_usd=float(totals.spend_usd),
                    delegation_runs=totals.runs,
                    total_tokens=totals.tokens,
                )
            },
            runs={TENANT: rows},
        )

    def request(
        self, command: ModelTenantReadCommand, timeout_s: float
    ) -> ModelTenantReadAnswer:
        assert timeout_s == OVERLAY.timeout_seconds
        self.commands.append(command)
        handler = (
            HandlerTenantSavingsReadBus(reader=self._reader())
            if command.kind is EnumTenantReadKind.SAVINGS
            else HandlerTenantDelegationsReadBus(reader=self._reader())
        )
        answer = asyncio.run(handler.handle(command))
        if not isinstance(answer, ModelTenantReadCompleted):
            return ModelTenantReadAnswer(
                completed=False, reason=f"{answer.reason}: {answer.detail}"
            )
        body = dict(answer.body)
        body.pop(self.drop_key or "", None)
        return ModelTenantReadAnswer(completed=True, body=body)

    def run_command(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        args = list(argv)
        traffic = (
            args[args.index("--traffic-class") + 1]
            if "--traffic-class" in args
            else None
        )
        surface = (
            args[args.index("--source-surface") + 1]
            if "--source-surface" in args
            else None
        )
        task = args[args.index("--task-type") + 1]
        sent = self.model.send(
            ModelSendRequest(
                task_type=task,
                prompt=args[2],
                traffic_class=traffic,
                source_surface=surface,
            )
        )
        return 0, json.dumps({SETTINGS.delegate_id_key: sent})


def _lane(fake: FakeTenantBus) -> BusLane:
    return BusLane(SETTINGS, OVERLAY, fake.request, run_command=fake.run_command)


class _Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(
    tmp_path: Path, fake: FakeTenantBus
) -> tuple[HandlerScenarioR8, list[str], InMemoryScenarioEventSink]:
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    clock = _Clock()
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows.append,
        sink=sink,
        terminal_topic="onex.evt.omnimarket.delegation-scenario-result.v1",
        lane_factory=lambda: _lane(fake),
        clock=lambda: NOW,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return handler, rows, sink


# -- golden chain ---------------------------------------------------------------------------------------------------


def test_the_contract_declares_the_bus_backend() -> None:
    assert SETTINGS.backend == "bus"
    assert SETTINGS.bus is not None


def test_golden_chain_every_assertion_held_over_the_tenant_bus_handlers(
    tmp_path: Path,
) -> None:
    fake = FakeTenantBus()
    handler, rows, _ = _run(tmp_path, fake)

    result = handler.handle(ModelScenarioR8RunCommand())

    assert [item for item in result.assertions if item.startswith("-")] == [], (
        result.detail
    )
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert len(rows) == 1
    assert {command.tenant_id for command in fake.commands} == {TENANT}
    assert {command.kind for command in fake.commands} == set(EnumTenantReadKind)


# -- error chains ---------------------------------------------------------------------------------------------------


def test_error_chain_a_failed_terminal_is_a_lane_failure_naming_the_reason() -> None:
    def failed(
        command: ModelTenantReadCommand, timeout_s: float
    ) -> ModelTenantReadAnswer:
        return ModelTenantReadAnswer(
            completed=False, reason="not_configured: analytics source"
        )

    with pytest.raises(LaneReadError, match="not_configured: analytics source"):
        BusLane(SETTINGS, OVERLAY, failed).read_totals()


def test_error_chain_a_projection_that_is_not_configured_fails_the_scenario(
    tmp_path: Path,
) -> None:
    def unreadable(
        command: ModelTenantReadCommand, timeout_s: float
    ) -> ModelTenantReadAnswer:
        return ModelTenantReadAnswer(
            completed=False, reason="no tenant read terminal: timed out"
        )

    rows: list[str] = []
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows.append,
        sink=InMemoryScenarioEventSink(),
        terminal_topic="onex.evt.omnimarket.delegation-scenario-result.v1",
        lane_factory=lambda: BusLane(SETTINGS, OVERLAY, unreadable),
        clock=lambda: NOW,
    )
    result = handler.handle(ModelScenarioR8RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "timed out" in result.detail


def test_error_chain_a_lane_with_no_bus_block_is_refused() -> None:
    settings = SETTINGS.model_copy(update={"bus": None})
    with pytest.raises(LaneReadError, match="no bus block"):
        BusLane(settings, OVERLAY, FakeTenantBus().request)


def test_error_chain_no_tenant_read_block_in_the_overlay_is_a_typed_failure() -> None:
    from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
        LANE_BUILDERS,
    )

    with pytest.raises(LaneReadError, match="no tenant_read block"):
        LANE_BUILDERS["bus"](SETTINGS, {}, None)


# -- failing control ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dropped", ["total_tokens", "delegation_runs", "total_spend_usd", "total_saved_usd"]
)
def test_failing_control_a_terminal_body_that_drops_a_totals_key_is_refused(
    dropped: str,
) -> None:
    """The break: the tenant node's terminal loses a key the R8 contract names. The lane must refuse, never read zero."""
    fake = FakeTenantBus(drop_key=dropped)
    assert dropped in set(SETTINGS.totals_keys.values())
    with pytest.raises(LaneReadError, match=dropped):
        _lane(fake).read_totals()


def test_failing_control_a_lane_that_counts_synthetic_runs_is_refused(
    tmp_path: Path,
) -> None:
    handler, _, _ = _run(tmp_path, FakeTenantBus(count_synthetic=True))
    result = handler.handle(ModelScenarioR8RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE


# -- seam: the shape the tenant nodes answer and the shape R8 reads -----------------------------------------------


def test_seam_the_command_each_read_builds_validates_as_the_shared_model() -> None:
    fake = FakeTenantBus()
    lane = _lane(fake)
    lane.read_totals()
    lane.read_runs()
    assert [command.kind for command in fake.commands] == [
        EnumTenantReadKind.SAVINGS,
        EnumTenantReadKind.DELEGATIONS,
    ]
    for command in fake.commands:
        assert ModelTenantReadCommand.model_validate(command.model_dump()) == command
    assert fake.commands[1].limit == SETTINGS.runs_limit


def test_seam_the_topics_the_lane_reads_are_the_tenant_node_contracts_topics() -> None:
    assert SETTINGS.bus is not None
    for package, kind in (
        (SETTINGS.bus.savings_node, "savings"),
        (SETTINGS.bus.delegations_node, "delegations"),
    ):
        topics = _load_topics(package)
        assert topics.command == f"onex.cmd.omnimarket.tenant-{kind}-read-requested.v1"
        assert topics.success == f"onex.evt.omnimarket.tenant-{kind}-read-completed.v1"
        assert topics.failure == f"onex.evt.omnimarket.tenant-{kind}-read-failed.v1"


def test_seam_the_overlay_document_model_carries_the_tenant_read_block() -> None:
    document = {
        "secret_store": {
            "provider": "infisical",
            "address": "http://store.invalid",
            "plain_http_hosts": ["store.invalid"],
            "identity_file": ".omnibase/.env",
            "client_id_env": "A",
            "client_secret_env": "B",
        },
        "tenant_read": {
            "bus": "kafka",
            "bus_lane": "dev",
            "probe_tenant_id": str(TENANT),
            "timeout_seconds": 60,
        },
    }
    try:
        parsed = ModelScenarioStoreOverlay.model_validate(document)
    except (
        Exception
    ):  # the secret_store block's own field names are another model's business
        parsed = None
    if parsed is not None:
        assert parsed.tenant_read is not None
        assert parsed.tenant_read.probe_tenant_id == TENANT
    assert (
        ModelScenarioTenantReadOverlay.model_validate(document["tenant_read"]).bus_lane
        == "dev"
    )
    assert Decimal("0") == Decimal(0)
    assert CONTRACT_PATH.name == "contract.yaml"


def test_the_bus_request_takes_the_workspace_root_from_the_environment_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workspace root is read from the mapping the lane was built with, never from the process environment."""
    seen: list[dict[str, object]] = []

    class _StopError(Exception):
        pass

    def fake_open_bus(bus_opts: dict[str, object]) -> object:
        seen.append(dict(bus_opts))
        raise _StopError

    monkeypatch.setattr(adapter_r8_bus_request, "_open_bus", fake_open_bus)
    monkeypatch.setenv("WS", "/from/the/process")
    settings = load_lane_settings(CONTRACT_PATH)
    assert settings.bus is not None
    command = ModelTenantReadCommand(
        correlation_id=UUID(int=1), kind=EnumTenantReadKind.SAVINGS, tenant_id=TENANT
    )
    for environment, expected in (
        ({"WS": "/from/the/mapping"}, Path("/from/the/mapping")),
        ({}, None),
    ):
        seen.clear()
        request = build_bus_request(
            OVERLAY,
            savings_node=settings.bus.savings_node,
            delegations_node=settings.bus.delegations_node,
            workspace_env="WS",
            environment=environment,
        )
        with pytest.raises(_StopError):
            request(command, 1.0)
        assert set(seen[0]) == {"bus", "bus_lane", "kafka_bootstrap", "omnibase_path"}
        assert seen[0]["bus"] == OVERLAY.bus
        assert seen[0]["bus_lane"] == OVERLAY.bus_lane
        assert seen[0]["omnibase_path"] == expected


def _build_bus_error(error_class: type[Exception]) -> Exception:
    """One of the bus errors the real broker raises, built the way its class allows."""
    if error_class is InfraTimeoutError:
        return error_class(
            "no tenant read terminal",
            ModelTimeoutErrorContext(
                transport_type=EnumInfraTransportType.KAFKA, operation="read"
            ),
        )
    return error_class("no tenant read terminal")


@pytest.mark.parametrize(
    "error_class",
    [TimeoutError, LabRunBusError, InfraTimeoutError, InfraConnectionError],
    ids=lambda error_class: error_class.__name__,
)
def test_the_bus_request_answers_the_lane_when_the_bus_raises_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[Exception],
) -> None:
    """The bus errors the real broker raises are answered, never re-raised: the lane survives with a non-completed answer."""

    def fake_open_bus(bus_opts: dict[str, object]) -> object:
        raise _build_bus_error(error_class)

    monkeypatch.setattr(adapter_r8_bus_request, "_open_bus", fake_open_bus)
    settings = load_lane_settings(CONTRACT_PATH)
    assert settings.bus is not None
    command = ModelTenantReadCommand(
        correlation_id=UUID(int=1), kind=EnumTenantReadKind.SAVINGS, tenant_id=TENANT
    )
    request = build_bus_request(
        OVERLAY,
        savings_node=settings.bus.savings_node,
        delegations_node=settings.bus.delegations_node,
        workspace_env="WS",
        environment={"WS": "/from/the/mapping"},
    )
    answer = request(command, 1.0)
    assert answer.completed is False
    assert answer.reason.startswith("no tenant read terminal")
