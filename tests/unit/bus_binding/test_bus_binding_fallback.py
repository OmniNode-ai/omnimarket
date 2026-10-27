# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable bus fails, the binding falls back to the in-memory bus, and nothing is lost (OMN-20275).

Each behaviour runs on both bindings. A falsifier proves the parity check fails when they differ.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.reducer import TRANSITIONS, reduce_binding_state
from omnimarket.handlers.bus_binding.select import BoundBus, open_bound_bus, read_state
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_api_key_async,
)
from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_bus_binding_reducer.handlers.handler_bus_binding_reducer import (
    HandlerBusBindingReducer,
)
from omnimarket.nodes.node_bus_binding_reducer.models import ModelBusBindingRequest

pytestmark = pytest.mark.unit

LANE = "dev"
SECRET = "GITHUB_TOKEN"
SOURCE_VAR = "OMN20275_PARITY_SOURCE"
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_bus_binding_reducer/contract.yaml"
)


class _Switch:
    """A durable bus that is up or down, and the in-memory bus the fallback opens."""

    def __init__(self) -> None:
        self.up = False
        self.opened: list[str] = []

    def __call__(
        self,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        root: Path | None,
    ) -> contextlib.AbstractAsyncContextManager[EventBusInmemory]:
        del lane, kafka_bootstrap, root
        return self._open(bus)

    @contextlib.asynccontextmanager
    async def _open(self, bus: str) -> AsyncIterator[EventBusInmemory]:
        self.opened.append(bus)
        if bus == "kafka" and not self.up:
            raise LabRunBusError(
                "InfraConnectionError: Unable to bootstrap from the closed port"
            )
        memory = EventBusInmemory(environment="local", group="bus-binding-test")
        await memory.start()
        try:
            yield memory
        finally:
            await memory.close()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    declaration = tmp_path / "config" / "lab" / "ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "default": "inmemory",
                "lanes": {
                    LANE: {
                        "broker": "127.0.0.1:1",
                        "security_protocol": "PLAINTEXT",
                        "binding_fallback": ["inmemory"],
                        "secret_resolver": {
                            "enable_convention_fallback": False,
                            "mappings": [
                                {
                                    "logical_name": SECRET,
                                    "source": {
                                        "source_type": "env",
                                        "source_path": SOURCE_VAR,
                                    },
                                }
                            ],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv(SOURCE_VAR, "not-a-real-token-value")
    monkeypatch.delenv(SECRET, raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    return tmp_path


def _bound(
    switch: _Switch, workspace: Path, state: Path, lines: list[str]
) -> contextlib.AbstractAsyncContextManager[BoundBus]:
    return open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        timeout_s=2.0,
        open_bus=switch,
        announce=lines.append,
        pending_outbox=lambda: outbox.pending_count(state),
    )


def test_fsm_contract_and_function_declare_the_same_transitions() -> None:
    machine = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["state_machine"]
    declared = {
        (EnumBusBindingState(t["from_state"]), t["trigger"]): EnumBusBindingState(
            t["to_state"]
        )
        for t in machine["transitions"]
    }
    assert declared == TRANSITIONS
    assert {s["state_name"] for s in machine["states"]} == {
        s.value for s in EnumBusBindingState
    }
    assert "degraded" in {s["state_name"] for s in machine["states"]}


@pytest.mark.parametrize("prior", list(EnumBusBindingState))
@pytest.mark.parametrize("healthy", [True, False])
def test_reducer_node_agrees_with_the_function(
    prior: EnumBusBindingState, healthy: bool
) -> None:
    state, binding = reduce_binding_state(prior, durable_healthy=healthy)
    result = HandlerBusBindingReducer().handle(
        ModelBusBindingRequest(prior_state=prior, durable_healthy=healthy)
    )
    assert (result.state, result.binding) == (state, binding)
    assert (binding is EnumBusBinding.INMEMORY) == (
        state is EnumBusBindingState.DEGRADED
    )


@pytest.mark.asyncio
async def test_down_bus_degrades_alarms_once_and_recovers_on_its_own(
    workspace: Path, tmp_path: Path
) -> None:
    switch, state, lines = _Switch(), tmp_path / "state", []
    async with _bound(switch, workspace, state, lines) as first:
        assert first.decision.binding is EnumBusBinding.INMEMORY
        assert first.decision.state is EnumBusBindingState.DEGRADED
    async with _bound(switch, workspace, state, lines) as second:
        assert second.decision.degraded
    alarms = [line for line in lines if " ALARM " in line]
    assert len(alarms) == 1, lines
    queue = (state / "exception-queue").rglob("*")
    assert any(p.is_file() for p in queue)
    assert any("binding=inmemory state=degraded" in line for line in lines)
    assert read_state(state, LANE) is EnumBusBindingState.DEGRADED

    switch.up = True
    async with _bound(switch, workspace, state, lines) as third:
        assert third.decision.state is EnumBusBindingState.RECOVERING
        assert third.decision.state.value == "recovering"
        assert third.decision.binding is EnumBusBinding.DURABLE
    async with _bound(switch, workspace, state, lines) as fourth:
        assert fourth.decision.state is EnumBusBindingState.HEALTHY
    assert read_state(state, LANE) is EnumBusBindingState.HEALTHY
    assert any("RECOVERY" in line for line in lines)


@pytest.mark.asyncio
async def test_a_lane_without_a_declared_fallback_fails_instead_of_degrading(
    workspace: Path, tmp_path: Path
) -> None:
    declaration = workspace / "config" / "lab" / "ci_bus_lanes.yaml"
    raw = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    del raw["lanes"][LANE]["binding_fallback"]
    declaration.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(LabRunBusError):
        async with _bound(_Switch(), workspace, tmp_path / "state", []):
            pass


async def _resolve_on(binding_up: bool, workspace: Path, tmp_path: Path) -> str:
    switch, state = _Switch(), tmp_path / f"state-{binding_up}"
    switch.up = binding_up
    async with _bound(switch, workspace, state, []) as bound:
        assert bound.decision.degraded is (not binding_up)
        secret = await resolve_api_key_async(SECRET)
    assert secret is not None
    return secret.get_secret_value()


@pytest.mark.asyncio
async def test_the_same_secret_resolves_on_both_bindings(
    workspace: Path, tmp_path: Path
) -> None:
    durable = await _resolve_on(True, workspace, tmp_path)
    fallback = await _resolve_on(False, workspace, tmp_path)
    assert durable == fallback


@pytest.mark.asyncio
async def test_falsifier_without_the_injected_resolver_the_in_memory_route_fails(
    workspace: Path, tmp_path: Path
) -> None:
    """The defect this change fixes: outside the binding, the process has no GITHUB_TOKEN to read."""
    with pytest.raises(SecretResolutionError):
        await resolve_api_key_async(SECRET)


def _request(rows: str = "x") -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=uuid.uuid4(),
        rows=rows,
        requested_by_lane="lane-a",
        requesting_host="host-a",
        requested_at=datetime.now(UTC),
    )


class _LedgerHost:
    """The serve side: appends once per request id and answers duplicate afterwards."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self._seen: set[uuid.UUID] = set()

    async def append(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        status = EnumWorkLedgerAppendStatus.DUPLICATE
        if request.request_id not in self._seen:
            self._seen.add(request.request_id)
            self.rows.append(request.rows)
            status = EnumWorkLedgerAppendStatus.ACCEPTED
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=status,
            exit_code=0,
            message="",
            ledger_lines=[len(self.rows)],
            ledger_host="h",
            duration_ms=1,
        )


async def _append(
    binding: str, request: ModelWorkLedgerAppendRequest, host: _LedgerHost, state: Path
) -> None:
    if binding == "durable":
        await host.append(request)
    else:
        outbox.queue_request(state, request)


async def _scenario(binding: str, state: Path) -> list[str]:
    host = _LedgerHost()
    requests = [_request(f"row-{n}") for n in range(3)]
    for request in requests:
        await _append(binding, request, host, state)
    if binding != "durable":
        assert outbox.pending_count(state) == 3
        first = await outbox.drain(state, host.append)
        assert (first.replayed, first.remaining) == (3, 0)
        # A request whose first attempt did land before the bus went down, queued again: one row only.
        await host.append(requests[0])
        outbox.queue_request(state, requests[0])
        again = await outbox.drain(state, host.append)
        assert again.duplicates == 1
        assert again.remaining == 0
    return sorted(host.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["durable", "fallback"])
async def test_ledger_rows_land_once_on_either_binding(
    binding: str, tmp_path: Path
) -> None:
    assert await _scenario(binding, tmp_path / binding) == ["row-0", "row-1", "row-2"]


@pytest.mark.asyncio
async def test_falsifier_parity_check_fails_when_a_binding_loses_a_row(
    tmp_path: Path,
) -> None:
    durable = await _scenario("durable", tmp_path / "a")
    lossy = await _scenario("fallback", tmp_path / "b")
    lossy.pop()
    assert durable != lossy


@pytest.mark.asyncio
async def test_a_failed_replay_loses_nothing(tmp_path: Path) -> None:
    request = _request()
    outbox.queue_request(tmp_path, request)
    outbox.queue_request(tmp_path, request)

    async def down(_: ModelWorkLedgerAppendRequest) -> ModelWorkLedgerAppendReceipt:
        raise TimeoutError

    result = await outbox.drain(tmp_path, down)
    assert result.replayed == 0
    assert result.remaining == 1
    assert result.stopped is not None
    json.loads(
        next(iter(outbox.outbox_dir(tmp_path).glob("*.json"))).read_text(
            encoding="utf-8"
        )
    )
