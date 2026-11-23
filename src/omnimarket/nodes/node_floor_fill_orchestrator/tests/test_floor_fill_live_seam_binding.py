# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile binds all four seams to their live sources, so a tick over a recorded snapshot places lanes.

The h051 tick printed ``UNKNOWN pool-unread:unbound:pool, brake-unread:unbound:brake, stop-signal-unread:unbound:stop-signal``
and exited 3 because ``build_live_handler_factory`` left the pool, tickets, brake and stop seams ``None`` and the entry
never bound them. Each seam had a reader on its own side (``SnapshotFloorPool`` was proved over a fill tick by its own
tests), and no test ran the live-bound fill.

The snapshot is recorded data:

* the floor record is the recorded floor record fixture ``factory-floor-2026-11-02T1040Z.json`` of the candidates compute;
* the pool reading is the lane slots of the ledger STATUS row of ``src=host-reading`` at 2026-11-23T09:25:23Z
  (``slots=h101:1,h202:14,h201:12,h105:3,h051:25,h053:12``), written through the pool snapshot writer's own file store;
  the stamp of the record is the harness's, because the writer's stamp is the time of the read;
* the plans the lines name are written from the plan shape of the ticket payload compute's own test support, one task
  section per ready task of the recorded record (the recorded floor record carries no plan text).

``test_live_binding_plans_lanes_*`` is the first failing control (the live factory over this snapshot starts at least one
lane), ``test_no_unbound_live_seam_*`` the poka-yoke (the live factory built with no injected seam binds all four), and
``test_live_seam_read_failure_*`` the read-failure rule (a source that cannot be read names itself and starts nothing).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedStarter,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import OVERLAY
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
    live_section,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.tests.support import (
    GOOD_TASK,
    HEADER,
)

pytestmark = pytest.mark.unit

OK = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]

NODES = Path(__file__).resolve().parents[2]
RECORDED_FLOOR_RECORD = (
    NODES
    / "node_floor_fill_candidates_compute"
    / "tests"
    / "fixtures"
    / "factory-floor-2026-11-02T1040Z.json"
)
#: ``slots=`` of the ledger STATUS row ``src=host-reading`` of 2026-11-23T09:25:23Z, per host.
RECORDED_SLOTS = {"h101": 1, "h202": 14, "h201": 12, "h105": 3, "h051": 25, "h053": 12}
#: The wall time of the tick: one minute after the recorded floor record was generated, so the record reads fresh.
PINNED = datetime(2026, 11, 2, 10, 41, tzinfo=UTC)


def _record_text() -> str:
    return RECORDED_FLOOR_RECORD.read_text(encoding="utf-8")


def _plans(record_text: str) -> dict[str, str]:
    """One plan per line of the record: the plan header and a task section for each ready task, each starting at once."""
    plans: dict[str, str] = {}
    for line in json.loads(record_text)["lines"]:
        sections = [
            GOOD_TASK.replace("### T1.", f"### {item['id']}.", 1).replace(
                "- **Files:**", "- **Start when:** none\n- **Files:**", 1
            )
            for station in line["stations"]
            for item in station["items"]
            if item["state"] == "prep" and item["note"].startswith("ready")
        ]
        plans[line["line_id"]] = HEADER + "\n" + "\n".join(sections)
    return plans


def _write_pool(
    path: Path,
    slots: dict[str, int],
    *,
    read_at: datetime,
    refusal: str | None = None,
) -> None:
    from omnibase_spi.contracts.services import (
        ModelLaneHostPoolReading,
        ModelLaneHostPoolSnapshot,
    )

    from omnimarket.models.model_pool_snapshot import (
        ModelPoolHostRow,
        ModelPoolSnapshotRecord,
    )
    from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
        FileSnapshotStore,
    )

    snapshot = ModelLaneHostPoolSnapshot(
        readings=tuple(
            ModelLaneHostPoolReading(
                name=host, lane_slots=count, lane_admission_refusal=refusal
            )
            for host, count in slots.items()
        )
    )
    record = ModelPoolSnapshotRecord(
        correlation_id=uuid.uuid4(),
        read_at=read_at,
        max_age_seconds=180,
        hosts=tuple(
            ModelPoolHostRow(
                host=host,
                declared_slots=count,
                running_lanes=0,
                free_slots=count,
                accounts_usable=(),
                accounts_limited=(),
                away=False,
            )
            for host, count in slots.items()
        ),
        free_slots_total=sum(slots.values()),
        snapshot_path=str(path),
        provenance="ledger host-reading STATUS 2026-11-23T09:25:23Z",
    )
    FileSnapshotStore(path, ".read-at.json").write(snapshot, record)


def _stop_facts(*stops: Any, enforce: tuple[str, ...] = ()) -> Any:
    from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract
    from omnimarket.models.stop_gate import EnumStopMode, ModelStopPolicy
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveStopFacts,
    )

    policy = ModelStopPolicy(
        modes=dict.fromkeys(enforce, EnumStopMode.ENFORCE),
        enforce_citations=dict.fromkeys(enforce, "test-citation"),
    )
    fsm = load_andon_stop_contract()[0]
    return lambda: LiveStopFacts(stops=tuple(stops), policy=policy, fsm=fsm, now=PINNED)


def _open_stop(cls: str, kind: str, key: str) -> Any:
    from omnimarket.models.stop_gate import ModelStopRecord, ModelStopScope

    return ModelStopRecord(
        id=f"stop-{cls}",
        cls=cls,
        scope=ModelStopScope(kind=kind, key=key),
        state="open",
        stopped_at=PINNED - timedelta(minutes=30),
    )


def _live_tick(
    tmp_path: Path,
    *,
    sources: Any = None,
    slots: dict[str, int] | None = None,
    pool_age: timedelta = timedelta(seconds=30),
    write_pool: bool = True,
    **bindings: Any,
) -> tuple[str, int, ScriptedStarter]:
    """A live tick over the recorded floor record and pool reading; nothing is injected unless ``bindings`` or ``sources`` say."""
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
        build_live_handler_factory,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
        ProtocolFloorClock,
    )
    from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
        InMemoryFloorTicketStore,
    )

    record_text = _record_text()
    plans = _plans(record_text)
    settings = load_floor_fill_live_settings(live_section(tmp_path))
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir(exist_ok=True)
    if write_pool:
        _write_pool(
            tmp_path / "pool.json",
            RECORDED_SLOTS if slots is None else slots,
            read_at=PINNED - pool_age,
        )

    class PinnedClock:
        def now(self) -> datetime:
            return PINNED

    clock: ProtocolFloorClock = PinnedClock()
    starter = ScriptedStarter()
    result = run_entry(
        ["tick"],
        {"floor_fill": OVERLAY},
        host_id=OVERLAY["host_id"],
        hosts=[(OVERLAY["host_id"], True)],
        build_handler=build_live_handler_factory(
            settings,
            dsn="unused",
            store=InMemoryFloorStateStore(),
            fetch_text=lambda _url, _timeout: record_text,
            read_file=lambda path: plans[os.path.basename(path).removesuffix(".md")],
            runner=starter,
            clock=clock,
            sources=sources
            or LiveSeamSources(
                ticket_store=lambda: InMemoryFloorTicketStore(),
                stop_facts=_stop_facts(),
            ),
            **bindings,
        ),
    )
    return result.stdout, result.exit_code, starter


def test_live_binding_plans_lanes_over_the_recorded_snapshot(tmp_path: Path) -> None:
    """The live factory with no injected seam reads the recorded pool and floor record and starts at least one lane."""
    out, code, starter = _live_tick(tmp_path)
    assert "unbound" not in out
    assert "unread" not in out
    assert code == OK
    assert len(starter.starts) >= 1
    assert len(starter.starts) <= sum(RECORDED_SLOTS.values())


def test_live_binding_plans_lanes_no_more_than_the_free_slots(tmp_path: Path) -> None:
    """With two recorded free slots the fill starts two lanes and no third: the pool reading is the budget."""
    _out, code, starter = _live_tick(tmp_path, slots={"h051": 2})
    assert code == OK
    assert len(starter.starts) == 2


def test_no_unbound_live_seam_in_the_live_factory(tmp_path: Path) -> None:
    """Built with no injected seam, the live factory binds a pool, tickets, brake and stop of its own: none is unbound."""
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
        build_live_handler_factory,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.models import (
        load_floor_fill_config,
    )

    settings = load_floor_fill_live_settings(live_section(tmp_path))
    handler = build_live_handler_factory(
        settings, dsn="unused", store=InMemoryFloorStateStore()
    )(load_floor_fill_config(OVERLAY))
    assert handler.unbound_seams() == ()


def test_no_unbound_live_seam_control_names_a_stand_in() -> None:
    """The failing control of the check: a pool or tickets stand-in, or a brake or stop left ``None``, is named as unbound."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_facts_live import (
        LiveFloorFacts,
        read_text_file,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.unbound_ports import (
        UnboundFloorPool,
        UnboundFloorTickets,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import harness

    orchestrator = harness(
        pool=UnboundFloorPool(), tickets=UnboundFloorTickets()
    ).orchestrator()
    assert orchestrator.unbound_seams() == ("pool", "tickets")
    facts = LiveFloorFacts(
        fetch_text=lambda _u, _t: "{}",
        read_file=read_text_file,
        timeout_seconds=5.0,
        plan_file_pattern="{line_id}.md",
        brake=None,
        stop=None,
    )
    assert facts.unbound_seams() == ("brake", "stop-signal")


def test_no_unbound_live_seam_entry_binds_the_factory_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live entry names its unbound-seam check: ``--check-live-binding`` builds the live seams and prints what it bound."""
    from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
        main,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
        full_overlay,
    )

    monkeypatch.setenv("EXAMPLE_FLOOR_FILL_DSN", "postgresql://unused")
    code = main(
        [
            "--overlay-json",
            json.dumps(full_overlay(tmp_path)),
            "--this-host",
            "fill-host",
            "--profile",
            "live",
            "--check-live-binding",
        ]
    )
    assert capsys.readouterr().out == (
        "OK live-binding: pool, tickets, brake, stop-signal bound\n"
    )
    assert code == 0


def test_live_seam_read_failure_pool_snapshot_absent_is_not_zero_slots(
    tmp_path: Path,
) -> None:
    """No snapshot at all is ``pool-unread``, never an empty pool: the pass starts nothing and is DEGRADED."""
    out, code, starter = _live_tick(tmp_path, write_pool=False)
    assert "pool-unread:pool-snapshot" in out
    assert code == DEGRADED
    assert starter.starts == []


def test_live_seam_read_failure_pool_snapshot_stale(tmp_path: Path) -> None:
    out, code, starter = _live_tick(tmp_path, pool_age=timedelta(seconds=181))
    assert "pool-unread:pool-snapshot" in out
    assert "stale" in out
    assert code == DEGRADED
    assert starter.starts == []


def test_live_seam_read_failure_pool_snapshot_without_hosts_is_not_zero_slots(
    tmp_path: Path,
) -> None:
    out, code, starter = _live_tick(tmp_path, slots={})
    assert "pool-unread:pool-snapshot:no-host-readings" in out
    assert code == DEGRADED
    assert starter.starts == []


def test_live_seam_read_failure_stop_queue_unreadable_names_brake_and_stop(
    tmp_path: Path,
) -> None:
    """A stop queue that cannot be read is an unread brake and an unread stop signal, never an open brake."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
    )
    from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
        InMemoryFloorTicketStore,
    )

    def unreadable() -> Any:
        raise OSError("queue file locked")

    out, code, starter = _live_tick(
        tmp_path,
        sources=LiveSeamSources(
            ticket_store=lambda: InMemoryFloorTicketStore(), stop_facts=unreadable
        ),
    )
    assert "brake-unread:andon-stops:OSError" in out
    assert "stop-signal-unread:andon-stops:OSError" in out
    assert "locked" not in out
    assert code == DEGRADED
    assert starter.starts == []


def test_live_seam_read_failure_ticket_store_unreadable_names_the_store(
    tmp_path: Path,
) -> None:
    """A ticket store that cannot be built (no Linear credential) mints nothing, starts nothing and names ``ticket-store``."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
    )

    def no_credential() -> Any:
        raise KeyError("LINEAR_API_KEY")

    out, _code, starter = _live_tick(
        tmp_path,
        sources=LiveSeamSources(ticket_store=no_credential, stop_facts=_stop_facts()),
    )
    assert "ticket-store:KeyError" in out
    assert "LINEAR_API_KEY" not in out
    assert starter.starts == []


def test_live_seam_read_failure_control_the_stop_gate_refusal_holds_the_fill(
    tmp_path: Path,
) -> None:
    """The positive control of the stop reader: an enforced engine stop is read as a stop signal and starts nothing."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
    )
    from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
        InMemoryFloorTicketStore,
    )

    stop = _open_stop("engine-policy-breach", "engine", "claude")
    out, _code, starter = _live_tick(
        tmp_path,
        sources=LiveSeamSources(
            ticket_store=lambda: InMemoryFloorTicketStore(),
            stop_facts=_stop_facts(stop, enforce=("engine-policy-breach",)),
        ),
    )
    assert "stop-signal" in out
    assert starter.starts == []


def test_live_seam_read_failure_control_the_landing_stop_is_the_brake(
    tmp_path: Path,
) -> None:
    """An enforced stop of the landing class holds new lanes as the open-count brake and starts nothing."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
    )
    from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
        InMemoryFloorTicketStore,
    )

    stop = _open_stop("repository-without-lander", "repository", "example/repo")
    out, _code, starter = _live_tick(
        tmp_path,
        sources=LiveSeamSources(
            ticket_store=lambda: InMemoryFloorTicketStore(),
            stop_facts=_stop_facts(stop, enforce=("repository-without-lander",)),
        ),
    )
    assert "open-count" in out
    assert starter.starts == []


def test_live_seam_read_failure_control_an_observed_stop_does_not_hold(
    tmp_path: Path,
) -> None:
    """A stop whose class is in observe mode is named by the gate and admitted: the fill is not held by it."""
    from omnimarket.models.stop_gate import EnumStopMode, ModelStopPolicy
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        LiveSeamSources,
        LiveStopFacts,
    )
    from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
        InMemoryFloorTicketStore,
    )

    stop = _open_stop("engine-policy-breach", "engine", "claude")
    base = _stop_facts(stop)()
    observed = LiveStopFacts(
        stops=base.stops,
        policy=ModelStopPolicy(
            modes={"engine-policy-breach": EnumStopMode.OBSERVE},
        ),
        fsm=base.fsm,
        now=PINNED,
    )
    _out, code, starter = _live_tick(
        tmp_path,
        sources=LiveSeamSources(
            ticket_store=lambda: InMemoryFloorTicketStore(),
            stop_facts=lambda: observed,
        ),
    )
    assert code == OK
    assert len(starter.starts) >= 1


def test_live_start_when_line_of_a_task_section_is_read_from_the_plan() -> None:
    """The start criterion of a task is the ``Start when:`` line of its own section, labelled; another task's line is not read."""
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
        _start_when,
    )

    plan = (
        "# Plan\n\n### T1. First\n\n- **Start when:** piece 1 reads WORKING\n\n"
        "### T2. Second\n\n- **Files:** x\n"
    )
    assert _start_when(plan, "T1") == "Start when: piece 1 reads WORKING"
    assert _start_when(plan, "T2") is None
    assert _start_when(plan, "T9") is None
