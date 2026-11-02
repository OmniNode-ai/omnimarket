# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Third-lease stop: the gate, its configuration, the replay over recorded rows, and bus parity.

The fixture ``cause_lease_rows.txt`` is the cause-lease CLAIM rows of the rolling work ledger as read at
2026-11-02T02:35Z, reduced to stamp, lane and cause, with two private repository names replaced by neutral ones (the same name in every row, so no
cause changes identity): every lease of every cause that was leased in the four
days before that time, with the earlier leases of those causes (a lease number needs the leases before it).
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_third_lease_stop_compute.handlers import (
    handler_third_lease_stop as handler_module,
)
from omnimarket.nodes.node_third_lease_stop_compute.handlers.handler_third_lease_stop import (
    HandlerThirdLeaseStop,
)
from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    EnumThirdLeaseStopDecision,
    EnumThirdLeaseStopMode,
    ModelThirdLeaseStopRequest,
)
from omnimarket.nodes.node_third_lease_stop_compute.overlay import (
    declared_row_sources,
    load_third_lease_stop_config,
)
from omnimarket.nodes.node_third_lease_stop_compute.row_source import (
    publish_rows,
    read_rows,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[2] / "fixtures/third_lease_stop/cause_lease_rows.txt"
FIXTURE_NOW = datetime(2026, 11, 2, 2, 35, tzinfo=UTC)
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
CAUSE = "cause:Example/repo:abc123def456"
OTHER = "cause:Example/repo:999999999999"


def overlay(tmp_path: Path, **keys: object) -> dict[str, str]:
    """A ``third_lease_stop`` overlay holding exactly ``keys`` under a fresh root."""
    directory = tmp_path / "third_lease_stop"
    directory.mkdir(exist_ok=True)
    (directory / "overlay.yaml").write_text(
        "".join(f"{k}: {v}\n" for k, v in keys.items()), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def config(
    tmp_path: Path, count: int = 3, mode: str = "report-only", source: str = "memory"
):
    return load_third_lease_stop_config(
        overlay(tmp_path, lease_count=count, mode=mode, row_source=source)
    )


def stamp(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def lease(minutes: int, number: int, cause: str = CAUSE) -> list[str]:
    """One lease as the landing controller writes it: the cause lane's row and the worker's row."""
    return [
        f"{stamp(minutes)} | CLAIM | lane=landing-cause-L{number} | cause={cause}",
        f"{stamp(minutes + 1)} | CLAIM | lane=landing-L{number} | cause={cause}",
    ]


def record(minutes: int, cause: str = CAUSE) -> str:
    return f"{stamp(minutes)} | STATUS | lane=reader | go-and-see={cause} | read the live surface"


def run(cfg, rows: list[str], cause: str | None = CAUSE, now: datetime | None = None):
    return HandlerThirdLeaseStop().handle(
        ModelThirdLeaseStopRequest(
            rows=tuple(rows),
            now=now or T0 + timedelta(hours=6),
            config=cfg,
            cause=cause,
        )
    )


def only(result):
    (decision,) = result.decisions
    return decision


# ---- AC1: stop on the third lease without a record after the second, admit once it exists ----


def test_third_lease_stop_stops_without_record_and_admits_with_one(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    rows = [*lease(0, 1), *lease(10, 2)]
    stopped = only(run(cfg, rows))
    assert stopped.decision is EnumThirdLeaseStopDecision.STOP
    assert stopped.lease_count == 2
    admitted = only(run(cfg, [*rows, record(30)]))
    assert admitted.decision is EnumThirdLeaseStopDecision.ADMIT
    assert admitted.go_and_see_at == T0 + timedelta(minutes=30)


def test_third_lease_stop_record_before_the_second_lease_does_not_clear(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    rows = [*lease(0, 1), record(5), *lease(10, 2)]
    assert only(run(cfg, rows)).decision is EnumThirdLeaseStopDecision.STOP


def test_third_lease_stop_below_the_count_admits(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    assert only(run(cfg, lease(0, 1))).decision is EnumThirdLeaseStopDecision.ADMIT
    assert only(run(cfg, [])).decision is EnumThirdLeaseStopDecision.ADMIT


def test_third_lease_stop_the_pair_of_rows_is_one_lease(tmp_path: Path) -> None:
    decision = only(run(config(tmp_path), lease(0, 1)))
    assert decision.lease_count == 1


def test_third_lease_stop_fourth_lease_needs_a_record_after_the_third(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    rows = [*lease(0, 1), *lease(10, 2), record(15), *lease(20, 3)]
    assert only(run(cfg, rows)).decision is EnumThirdLeaseStopDecision.STOP
    assert (
        only(run(cfg, [*rows, record(40)])).decision is EnumThirdLeaseStopDecision.ADMIT
    )


def test_third_lease_stop_one_cause_does_not_stop_another(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    rows = [*lease(0, 1), *lease(10, 2), *lease(20, 3, OTHER)]
    assert (
        only(run(cfg, rows, cause=OTHER)).decision is EnumThirdLeaseStopDecision.ADMIT
    )
    sweep = run(cfg, rows, cause=None)
    by_cause = {d.cause: d.decision for d in sweep.decisions}
    assert by_cause == {
        CAUSE: EnumThirdLeaseStopDecision.STOP,
        OTHER: EnumThirdLeaseStopDecision.ADMIT,
    }


# ---- AC2: the count and the mode come from the contract and the overlay ----


@pytest.mark.parametrize(
    ("count", "expected"),
    [(2, EnumThirdLeaseStopDecision.STOP), (3, EnumThirdLeaseStopDecision.ADMIT)],
)
def test_third_lease_stop_config_count_decides(
    tmp_path: Path, count: int, expected: EnumThirdLeaseStopDecision
) -> None:
    """One lease held: the next is the second, which a count of 2 stops and a count of 3 admits."""
    assert only(run(config(tmp_path, count=count), lease(0, 1))).decision is expected


def test_third_lease_stop_config_mode_report_only_never_blocks(tmp_path: Path) -> None:
    rows = [*lease(0, 1), *lease(10, 2)]
    report = run(config(tmp_path, mode="report-only"), rows)
    assert only(report).decision is EnumThirdLeaseStopDecision.STOP
    assert not only(report).enforced
    assert not report.blocks
    enforce = run(config(tmp_path, mode="enforce"), rows)
    assert only(enforce).enforced
    assert enforce.blocks
    assert enforce.mode is EnumThirdLeaseStopMode.ENFORCE


@pytest.mark.parametrize("missing", ["lease_count", "mode", "row_source"])
def test_third_lease_stop_config_absent_key_refuses_naming_it(
    tmp_path: Path, missing: str
) -> None:
    keys = {"lease_count": 3, "mode": "report-only", "row_source": "memory"}
    del keys[missing]
    with pytest.raises(NodeOverlayError, match=missing):
        load_third_lease_stop_config(overlay(tmp_path, **keys))


def test_third_lease_stop_config_no_overlay_refuses_naming_the_overlay(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match="third_lease_stop"):
        load_third_lease_stop_config({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_third_lease_stop_config_undeclared_row_source_refuses(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match="row_source"):
        config(tmp_path, source="file")
    assert set(declared_row_sources()) == {"memory", "bus"}


def test_third_lease_stop_config_handler_holds_no_configured_literal() -> None:
    """The count, the modes, the window and the grammar's cell names are not in the handler source."""
    tree = ast.parse(Path(handler_module.__file__).read_text(encoding="utf-8"))
    numbers = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int | float)
        and not isinstance(n.value, bool)
    }
    strings = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert numbers <= {0, 1}, numbers
    forbidden = {
        "report-only",
        "enforce",
        "CLAIM",
        "STATUS",
        "cause",
        "lane",
        "go-and-see",
        "landing-",
    }
    assert not [s for s in strings if s in forbidden], strings & forbidden


# ---- AC3: report-only emits one would-stop per refused lease; replay over the recorded rows ----


def test_third_lease_stop_replay_one_would_stop_per_lease_past_the_count(
    tmp_path: Path,
) -> None:
    rows = [*lease(0, 1), *lease(10, 2), *lease(20, 3), *lease(30, 4)]
    result = run(config(tmp_path), rows)
    assert [(w.lease_number, w.lease_id) for w in result.would_stop] == [
        (3, "L3"),
        (4, "L4"),
    ]
    assert all(w.mode is EnumThirdLeaseStopMode.REPORT_ONLY for w in result.would_stop)
    assert not result.blocks
    assert "would_stop=2" in result.line
    with_record = run(config(tmp_path), [*rows, record(25)])
    # the record after lease 3 clears lease 4; lease 3 had none after lease 2
    assert [w.lease_id for w in with_record.would_stop] == ["L3"]


def test_third_lease_stop_replay_window_counts_only_recent_leases(
    tmp_path: Path,
) -> None:
    rows = [*lease(0, 1), *lease(10, 2), *lease(20, 3)]
    late = T0 + timedelta(hours=200)
    assert run(config(tmp_path), rows, now=late).would_stop == ()


def test_third_lease_stop_replay_recorded_rows(tmp_path: Path) -> None:
    """The recorded four days: no go-and-see record exists on the ledger, so every lease from the third on counts."""
    rows = FIXTURE.read_text(encoding="utf-8").splitlines()
    result = run(config(tmp_path), rows, cause=None, now=FIXTURE_NOW)
    assert result.records_seen == 0
    assert result.rows_unreadable == 0
    assert (result.causes_seen, result.leases_seen, len(result.would_stop)) == (
        103,
        842,
        295,
    )
    assert len({(w.cause, w.lease_id) for w in result.would_stop}) == 295
    assert not result.blocks
    assert "would_stop=295" in result.line


def test_third_lease_stop_replay_count_line(tmp_path: Path) -> None:
    """The node's count line over the recorded rows, as the packaged runtime prints it in the result."""
    rows = FIXTURE.read_text(encoding="utf-8").splitlines()
    result = run(config(tmp_path), rows, cause=None, now=FIXTURE_NOW)
    assert result.line == (
        "third_lease_stop mode=report-only lease_count=3 window_hours=96 causes=103 "
        "would_stop=295 gate_stopped=53 leases=842 records=0 rows_unreadable=0"
    )


# ---- AC4: the same decisions from the in-memory source and from a bus ----


async def bus_decisions(tmp_path: Path, bus) -> tuple[object, object]:
    rows = FIXTURE.read_text(encoding="utf-8").splitlines()
    cfg_memory = config(tmp_path, source="memory")
    cfg_bus = config(tmp_path, source="bus")
    # The in-memory bus keeps no history: the reader subscribes first, then the rows are published.
    reader = asyncio.create_task(
        read_rows(cfg_bus, bus=bus, expected=len(set(rows)), group_id=f"tls-{id(bus)}")
    )
    await asyncio.sleep(0.5)
    await publish_rows(bus, rows)
    via_bus = await reader
    via_memory = await read_rows(cfg_memory, memory_rows=rows)
    assert len(set(via_bus)) == len(set(rows))
    return (
        run(cfg_memory, list(via_memory), cause=None, now=FIXTURE_NOW),
        run(cfg_bus, list(via_bus), cause=None, now=FIXTURE_NOW),
    )


def same_decisions(left, right) -> None:
    assert left.decisions == right.decisions
    assert left.would_stop == right.would_stop
    assert (left.causes_seen, left.leases_seen) == (
        right.causes_seen,
        right.leases_seen,
    )


async def test_third_lease_stop_parity_inmemory_bus(tmp_path: Path) -> None:
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    bus = EventBusInmemory(environment="unit-test", group="third-lease-stop")
    await bus.start()
    try:
        memory, via_bus = await asyncio.wait_for(
            bus_decisions(tmp_path, bus), timeout=60
        )
    finally:
        await bus.close()
    same_decisions(memory, via_bus)
    assert len(via_bus.would_stop) == 295


@pytest.mark.integration
@pytest.mark.kafka
async def test_third_lease_stop_parity_redpanda(
    tmp_path: Path, kafka_integration_bus: EventBusKafka
) -> None:
    """The bus overlay on Redpanda: the same decisions as the memory source.

    Runs where the test lane's principal may write the contract's row topic; a refusal by the broker's
    ACLs fails the test with the broker's own message.
    """
    memory, via_bus = await asyncio.wait_for(
        bus_decisions(tmp_path, kafka_integration_bus), timeout=90
    )
    same_decisions(memory, via_bus)
    assert len(via_bus.would_stop) == 295
