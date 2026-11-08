# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23083: the publish cycle runs join J2 (piece 2 to piece 3) before the simple floor reads its receipts.

The join's check (a canary publishes one command, placement consumes the host's advertisement, one correlated result
returns) was built and tested and never run: nothing called ``join-j2`` outside a test, so the served board of
2026-11-08 05:07Z held no chain from it and J2 read broken on 28 unanswered piece 2 failures with no golden chain.

Golden chain: one publish run calls ``join-j2`` on the overlay's bus lane before the floor step, and the floor step still
runs; the journal the step writes makes the floor's fold read J2 golden. Error chains: a step that fails (the bus cannot be
read) is recorded failed, the floor still reads and holds no golden chain for J2; the overlay's own host list is what the
join runs for. Failing control: with the step dropped from the contract (the named injected break) no run calls the join
and the fold reads J2 not run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelPieceDef,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers import (
    handler_factory_floor_publish as handler_module,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_host_reading_effect import runtime_host_reading
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    FLOOR_SIMPLE,
    JOIN_J2,
    PLUGIN,
    Scripted,
    _refresh_request,
)

pytestmark = pytest.mark.unit

RT = load_runtime()
NOW = datetime(2026, 11, 8, 5, 30, tzinfo=UTC)


def _module_calls(runner: Scripted) -> list[tuple[str, list[str]]]:
    """Each ``python -m <module> <args>`` the run made, in order."""
    return [
        (c[0][2], c[0][3:]) for c in runner.calls if len(c[0]) > 2 and c[0][1] == "-m"
    ]


def _run(tmp_path: Path, code: int = 0) -> tuple[Scripted, list[tuple[str, str]]]:
    runner = Scripted(
        {"--json": (0, b"[]", b""), "join-j2": (code, b"", b"J2-BUS-FAILED x")}
    )
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    return runner, [(s.name, s.status) for s in result.steps]


def _chain(
    sink: InMemoryEmissionSink, node: str, piece: int, cid: str, *, ok: bool = True
) -> None:
    chain = ChainEmitter(
        sink,
        node=node,
        piece=piece,
        clock=lambda: NOW,
        correlation_id=cid,
        backend="real",
        drill=False,
    )
    chain.start()
    chain.success({}) if ok else chain.failure("canary_untyped", "x")


def _j2_state(sink: InMemoryEmissionSink) -> str:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(minutes=1),
            emissions=tuple(sink.events),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=3600)
                for p in (2, 3)
            ),
            joins=(
                ModelJoinDef(id="J2", from_piece=2, to_piece=3, label="hosts to bus"),
            ),
            freshness_max_age_s=900,
            error_max_age_s=3600,
            chain_deadline_s=900,
        )
    )
    return next(j.state for j in receipts.joins if j.join == "J2")


def test_golden_the_contract_declares_the_j2_step_before_the_floor_step() -> None:
    step = next(s for s in RT.steps if s.name == "join_j2")
    assert (step.kind, step.module) == ("module", JOIN_J2)
    assert step.args == ("join-j2", "--bus", "kafka", "--bus-lane", "{bus_lane}")
    assert step.concurrent
    assert not step.required
    names = [s.name for s in RT.steps]
    assert names.index("join_j2") < names.index("floor_simple")


def test_golden_one_publish_run_calls_the_join_before_the_floor_step(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path)
    calls = _module_calls(runner)
    modules = [m for m, _ in calls]
    assert modules.index(JOIN_J2) < modules.index(FLOOR_SIMPLE)
    args = dict(calls)[JOIN_J2]
    assert args[:3] == ["join-j2", "--bus", "kafka"]
    assert args[3] == "--bus-lane"
    assert dict(steps)["join_j2"] == "ok"
    assert dict(steps)["floor_simple"] == "ok"


def test_golden_the_step_adds_nothing_to_the_longest_declared_path() -> None:
    group = max(s.timeout_seconds for s in RT.steps if s.concurrent)
    assert RT.declared_longest_path()["input_steps"] == (
        group + sum(s.timeout_seconds for s in RT.steps if not s.concurrent)
    )
    assert next(s for s in RT.steps if s.name == "join_j2").timeout_seconds <= group
    assert sum(RT.declared_longest_path().values()) <= RT.cycle.bound_seconds


def test_golden_the_chains_the_step_writes_make_the_floor_read_j2_golden() -> None:
    sink = InMemoryEmissionSink()
    cid = "c" * 32
    _chain(sink, "node_host_reading_effect:join_j2", 2, cid)
    _chain(sink, "node_lab_placement_compute:join_j2", 3, cid)
    assert _j2_state(sink) == "golden_only"


def test_error_a_step_that_fails_is_recorded_and_the_floor_still_reads(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path, code=1)
    assert dict(steps)["join_j2"] == "failed"
    assert dict(steps)["floor_simple"] == "ok"
    assert FLOOR_SIMPLE in [m for m, _ in _module_calls(runner)]


def test_error_a_failed_canary_alone_never_reads_as_a_golden_join() -> None:
    sink = InMemoryEmissionSink()
    _chain(sink, "node_host_reading_effect:host_canary", 2, "d" * 32, ok=False)
    assert _j2_state(sink) != "golden_only"
    assert _j2_state(sink) != "passed"


def test_error_control_without_the_step_no_join_runs_and_the_fold_reads_j2_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = RT.model_copy(
        update={"steps": tuple(s for s in RT.steps if s.name != "join_j2")}
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: without)
    runner, steps = _run(tmp_path)
    assert JOIN_J2 not in [m for m, _ in _module_calls(runner)]
    assert "join_j2" not in dict(steps)
    # the hourly canary's own golden chain is all the journal holds: piece 3 never answers it
    sink = InMemoryEmissionSink()
    _chain(sink, "node_host_reading_effect:host_canary", 2, "e" * 32)
    assert _j2_state(sink) == "not_run"


class _FakeCaller:
    def __init__(self, bus: object, *, wait_slack_seconds: int) -> None:
        pass

    async def start(self) -> None:
        pass

    async def collect(self, seconds: int) -> None:
        pass

    async def stop(self) -> None:
        pass


def _run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hosts: tuple[str, ...]
) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []

    class _FakeJoin:
        def __init__(self, **_: Any) -> None:
            pass

        async def run_hosts(self, names: tuple[str, ...]) -> tuple[()]:
            seen.append(tuple(names))
            return ()

    @asynccontextmanager
    async def _bus(**_: Any) -> Any:
        yield object()

    overlay = SimpleNamespace(
        join_j2=SimpleNamespace(
            settings=SimpleNamespace(result_wait_slack_seconds=1),
            emission_home_env="J2_STEP_TEST_HOME",
            emission_journal_relpath="j2.jsonl",
            collect_window_seconds=0,
            hosts=hosts,
        )
    )
    monkeypatch.setattr(runtime_host_reading, "load_overlay", lambda: overlay)
    monkeypatch.setattr(runtime_host_reading, "open_lab_run_bus", _bus)
    monkeypatch.setattr(runtime_host_reading, "LabWorkCaller", _FakeCaller)
    monkeypatch.setattr(runtime_host_reading, "JoinJ2", _FakeJoin)
    monkeypatch.setattr(
        runtime_host_reading,
        "HostTableInventory",
        lambda _env: SimpleNamespace(
            hosts=lambda: [SimpleNamespace(name="pooled-x", pooled=True)]
        ),
    )
    monkeypatch.setenv("J2_STEP_TEST_HOME", str(tmp_path))
    runtime_host_reading.main(["join-j2", "--bus", "kafka"])
    return seen


def test_golden_the_overlay_host_list_is_what_the_join_runs_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run_main(monkeypatch, tmp_path, ("h-a", "h-b")) == [("h-a", "h-b")]


def test_error_control_an_empty_overlay_host_list_runs_every_pooled_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run_main(monkeypatch, tmp_path, ()) == [("pooled-x",)]
