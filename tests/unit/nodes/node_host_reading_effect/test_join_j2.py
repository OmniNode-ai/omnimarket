# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J2 (rebuild plan section 4.1, piece 2 to piece 3): a canary on each host publishes one command,
placement consumes the host's capacity advertisement and one correlated result returns on the bus.

Golden and error join tests on the in-memory bus, with the real pool pieces on it: ``LabWorkHost``
serves and advertises, ``LabWorkCaller`` places and sends. The receipts the floor reads are folded
from the emissions the join wrote, so the join shows what the floor would show.

Named break (the failing control): a host answers the command with a receipt for another unit. The
join must read that as a typed failure; a join that read it as success turns the control red.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost, load_lab_work_topics
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.models.model_join_j2 import (
    EnumJoinJ2Outcome,
    ModelJoinJ2HostResult,
    ModelJoinJ2Settings,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_join_j2 import (
    HandlerJoinJ2,
)
from omnimarket.nodes.node_lab_placement_compute.handlers import HandlerLabPoolPlace
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import HostReading

pytestmark = pytest.mark.unit

SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
POOLED = ("h-a", "h-b")
SILENT = "h-silent"
MARKER = "J2-CANARY"
SETTINGS = ModelJoinJ2Settings(
    canary_piece=2,
    seam_piece=3,
    canary_node="node_host_reading_effect",
    seam_node="node_lab_placement_compute",
    repo="OmniNode-ai/omnimarket",
    commit_sha=SHA,
    argv=("python3", "-c", f"print('{MARKER} {{nonce}}')"),
    result_marker=MARKER,
    kind="other",
    lane="host-canary",
    command_timeout_seconds=1,
    result_wait_slack_seconds=1,
    advert_max_age_seconds=60,
    need_tools=("git",),
)
TABLE = ModelLabHostsTable(
    hosts=tuple(ModelLabHostRow(name=n, target=n) for n in (*POOLED, SILENT))
)
NONCE = re.compile(rf"{MARKER} (\S+)'")


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.5, mem_available_bytes=32 * 1024**3, tools=("git", "uv")
        )


class _EchoRunner:
    """Runs the canary the way a host would: prints the marker and the nonce it was given."""

    def __init__(self, *, answer_for: str | None = None, exit_code: int = 0) -> None:
        self.ran: list[str] = []
        self._answer_for = answer_for
        self._exit = exit_code

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.ran.append(request.work_unit_id)
        found = NONCE.search(request.argv[2])
        assert found is not None
        return ModelLabWorkUnitReceipt(
            work_unit_id=self._answer_for or request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=self._exit,
            output_tail=f"{MARKER} {found.group(1)}\n",
            duration_seconds=0.1,
        )


class _World:
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="local", group="j2-join-test")
        self.hosts: dict[str, LabWorkHost] = {}
        self.runners: dict[str, _EchoRunner] = {}
        self.commands: list[bytes] = []
        self.caller: LabWorkCaller | None = None
        self.sink = InMemoryEmissionSink()

    async def serve(
        self, name: str, runner: _EchoRunner, *, serving: bool = True
    ) -> None:
        host = LabWorkHost(
            self.bus,
            HandlerLabWorkUnitEffect(name, runner),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
        )
        self.hosts[name], self.runners[name] = host, runner
        if serving:
            await host.start(advertise=False)
        await host.advertise_once()

    async def open(self) -> HandlerJoinJ2:
        topics = load_lab_work_topics()

        async def on_command(message: object) -> None:
            self.commands.append(getattr(message, "value", b""))

        await self.bus.subscribe(
            topics.command, on_message=on_command, group_id="j2-command-probe"
        )
        self.caller = LabWorkCaller(
            self.bus,
            wait_slack_seconds=SETTINGS.result_wait_slack_seconds,
            placer=HandlerLabPoolPlace(table_source=lambda: TABLE),
        )
        await self.caller.start()
        return HandlerJoinJ2(
            settings=SETTINGS,
            caller=self.caller,
            sink=self.sink,
            backend="in_memory",
        )

    async def close(self) -> None:
        if self.caller is not None:
            await self.caller.stop()
        for host in self.hosts.values():
            await host.stop()
        await self.bus.close()


def _run(
    scenario: Callable[[_World], Awaitable[object]],
) -> tuple[_World, object]:
    async def go() -> tuple[_World, object]:
        world = _World()
        await world.bus.start()
        try:
            return world, await scenario(world)
        finally:
            await world.close()

    return asyncio.run(go())


def _receipts(world: _World, *, now: datetime | None = None) -> object:
    emissions = tuple(world.sink.events)
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now or datetime.now(UTC) + timedelta(seconds=1),
            emissions=emissions,
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=3600)
                for p in (SETTINGS.canary_piece, SETTINGS.seam_piece)
            ),
            joins=(
                ModelJoinDef(
                    id="J2",
                    from_piece=SETTINGS.canary_piece,
                    to_piece=SETTINGS.seam_piece,
                    label="hosts to bus",
                ),
            ),
            freshness_max_age_s=900,
            error_max_age_s=3600,
            chain_deadline_s=30,
        )
    )


def _by_host(
    results: object,
) -> dict[str, ModelJoinJ2HostResult]:
    assert isinstance(results, tuple)
    return {r.host: r for r in results}


def _phases(world: _World, cid: str, piece: int) -> list[tuple[str, str | None]]:
    return [
        (e.phase, e.failure_code)
        for e in world.sink.events
        if e.correlation_id == cid and e.piece == piece
    ]


async def _golden_world(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
    handler = await world.open()
    for name in POOLED:
        await world.serve(name, _EchoRunner())
    return await handler.run_hosts(POOLED)


def test_golden_every_host_returns_one_correlated_result_and_the_floor_reads_j2_golden() -> (
    None
):
    world, results = _run(_golden_world)
    by_host = _by_host(results)
    assert set(by_host) == set(POOLED)
    for name, result in by_host.items():
        assert result.outcome is EnumJoinJ2Outcome.GOLDEN, result
        assert result.ok
        assert result.failure_code == ""
        # one command, to this host alone, and its own unit is what the host ran
        assert world.runners[name].ran == [result.work_unit_id]
        # the receipt that came back is for the command that went out, from the host addressed
        assert result.receipt_host == name
        assert result.advert_age_seconds is not None
        # canary piece, then the seam piece, under one correlation id, in order
        assert _phases(world, result.correlation_id, 2) == [
            ("start", None),
            ("success", None),
        ]
        assert _phases(world, result.correlation_id, 3) == [
            ("start", None),
            ("success", None),
        ]
    assert len(world.commands) == len(POOLED)
    assert len({r.correlation_id for r in by_host.values()}) == len(POOLED)
    receipts = _receipts(world)
    join = receipts.joins[0]  # type: ignore[attr-defined]
    assert join.join == "J2"
    assert join.state == "golden_only"
    assert join.golden_at is not None
    assert receipts.findings == ()  # type: ignore[attr-defined]


async def _no_advert_world(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
    handler = await world.open()
    await world.serve(POOLED[0], _EchoRunner())
    # SILENT is in the host table and pooled, and never advertised
    return await handler.run_hosts((SILENT,))


def test_error_a_host_with_no_advertisement_is_a_typed_failure_and_nothing_is_sent() -> (
    None
):
    world, results = _run(_no_advert_world)
    result = _by_host(results)[SILENT]
    assert result.outcome is EnumJoinJ2Outcome.NO_ADVERTISEMENT
    assert not result.ok
    assert result.failure_code == "canary_no_advertisement"
    assert result.work_unit_id == ""
    # no command went out for it, and the one host that did advertise ran nothing
    assert world.commands == []
    assert world.runners[POOLED[0]].ran == []
    cid = result.correlation_id
    assert _phases(world, cid, 2) == [
        ("start", None),
        ("failure", "canary_no_advertisement"),
    ]
    # the seam piece holds a typed upstream refusal and never started
    assert _phases(world, cid, 3) == [("failure", "upstream_canary_no_advertisement")]
    receipts = _receipts(world)
    join = receipts.joins[0]  # type: ignore[attr-defined]
    assert join.state == "error_only"
    assert join.error_at is not None
    assert join.golden_at is None
    assert receipts.findings == ()  # type: ignore[attr-defined]


def test_error_golden_then_error_makes_the_floor_read_j2_passed() -> None:
    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner())
        return await handler.run_hosts((POOLED[0], SILENT))

    world, results = _run(scenario)
    by_host = _by_host(results)
    assert by_host[POOLED[0]].outcome is EnumJoinJ2Outcome.GOLDEN
    assert by_host[SILENT].outcome is EnumJoinJ2Outcome.NO_ADVERTISEMENT
    join = _receipts(world).joins[0]  # type: ignore[attr-defined]
    assert join.state == "passed"
    assert join.golden_at is not None
    assert join.error_at is not None


def test_error_a_stale_advertisement_is_a_typed_failure_and_nothing_is_sent() -> None:
    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner())
        later = datetime.now(UTC) + timedelta(
            seconds=SETTINGS.advert_max_age_seconds * 3
        )
        stale = HandlerJoinJ2(
            settings=SETTINGS,
            caller=world.caller,  # type: ignore[arg-type]
            sink=world.sink,
            backend="in_memory",
            clock=lambda: later,
        )
        del handler
        return await stale.run_hosts((POOLED[0],))

    world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.STALE_ADVERTISEMENT
    assert result.failure_code == "canary_advert_stale"
    assert world.commands == []
    assert world.runners[POOLED[0]].ran == []
    assert _phases(world, result.correlation_id, 3) == [
        ("failure", "upstream_canary_advert_stale")
    ]


def test_error_an_advertising_host_that_does_not_serve_gives_no_result_and_is_not_success() -> (
    None
):
    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner(), serving=False)
        return await handler.run_hosts((POOLED[0],))

    world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.NO_RESULT
    assert not result.ok
    assert result.failure_code == "seam_no_result"
    assert len(world.commands) == 1
    assert _phases(world, result.correlation_id, 3) == [
        ("start", None),
        ("failure", "seam_no_result"),
    ]
    join = _receipts(world).joins[0]  # type: ignore[attr-defined]
    assert join.golden_at is None


def test_error_control_a_receipt_for_another_unit_is_not_read_as_the_result() -> None:
    """The named break: the host answers with a receipt whose unit is not the one sent."""

    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner(answer_for="lw-someone-elses-unit"))
        return await handler.run_hosts((POOLED[0],))

    world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.UNCORRELATED_RESULT
    assert not result.ok
    assert result.failure_code == "seam_result_uncorrelated"
    assert _phases(world, result.correlation_id, 3)[-1] == (
        "failure",
        "seam_result_uncorrelated",
    )
    join = _receipts(world).joins[0]  # type: ignore[attr-defined]
    assert join.golden_at is None


def test_error_a_canary_that_exits_nonzero_is_a_typed_failure() -> None:
    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner(exit_code=3))
        return await handler.run_hosts((POOLED[0],))

    _world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.COMMAND_FAILED
    assert result.failure_code == "seam_command_failed"


def test_golden_the_emission_failure_codes_are_typed_and_the_events_validate() -> None:
    world, _results = _run(_golden_world)
    assert all(isinstance(e, ModelNodeEmission) for e in world.sink.events)
    assert {e.node for e in world.sink.events} == {
        SETTINGS.canary_node,
        SETTINGS.seam_node,
    }
    assert all(e.backend == "in_memory" and not e.drill for e in world.sink.events)
