# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four live seams, each overridable by injection and each carrying a tick when bound.

The entry accepts the live profile (the dsn gate runs before any seam is bound). With the four seams
(pool, tickets, brake, stop) injected, a live tick ends OK; a brake that answers True and a stop that refuses hold the
pass and are named. The factory never leaves a seam unbound (``test_floor_fill_live_seam_binding``); the ``unbound``
tickets stand-in stays only as the failing control of the unbound-seam check, and answers its mint as ``unavailable``.
A record that the reader lane extends with a ``needs`` cell still reads, because the record model ignores unknown cells.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    ScriptedPool,
    ScriptedTickets,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.unbound_ports import (
    UnboundFloorTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    main,
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    OVERLAY,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
    full_overlay,
    live_section,
)

OK = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK]
FAILED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]


def all_bound() -> dict[str, Any]:
    """The four seams bound: a pool with two free slots, a scripted tickets, a brake that holds no lane, and a stop that refuses nothing."""
    return {
        "pool": ScriptedPool(2),
        "tickets": ScriptedTickets(),
        "brake": lambda: False,
        "stop": lambda _c: None,
    }


def tick_with(
    tmp_path: Path, *, record_text: str, **bindings: Any
) -> tuple[str, int, InMemoryFloorStateStore]:
    """A live tick over ``record_text`` with the given seams bound: the stdout, exit code, and store."""
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
        build_live_handler_factory,
    )

    store = InMemoryFloorStateStore()
    settings = load_floor_fill_live_settings(live_section(tmp_path))
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir()
    result = run_entry(
        ["tick"],
        {"floor_fill": OVERLAY},
        host_id=OVERLAY["host_id"],
        hosts=[(OVERLAY["host_id"], True)],
        build_handler=build_live_handler_factory(
            settings,
            dsn="unused",
            store=store,
            fetch_text=lambda _u, _t: record_text,
            **bindings,
        ),
    )
    return result.stdout, result.exit_code, store


def _fresh_record() -> str:
    """A floor record fresh against the wall clock, so the pass does not read it as stale."""
    return floor_record(LINES4, generated_at=datetime.now(UTC))


def test_live_profile_binding_is_accepted_by_the_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live profile is accepted: the dsn gate runs first and names its unset variable rather than refusing the profile."""
    monkeypatch.delenv("EXAMPLE_FLOOR_FILL_DSN", raising=False)
    code = main(
        [
            "--overlay-json",
            json.dumps(full_overlay(tmp_path)),
            "--this-host",
            "fill-host",
            "--profile",
            "live",
            "tick",
        ]
    )
    assert code == FAILED
    assert capsys.readouterr().out == (
        "UNKNOWN fill-state:store-dsn-unset:EXAMPLE_FLOOR_FILL_DSN\n"
    )


def test_live_profile_all_seams_bound_tick_is_ok(tmp_path: Path) -> None:
    """With all four seams bound, a live tick ends OK and prints nothing that names an unbound seam."""
    out, code, _store = tick_with(tmp_path, record_text=_fresh_record(), **all_bound())
    assert code == OK
    assert "unbound" not in out


def test_seam_pool_injected_overrides_the_live_reader(tmp_path: Path) -> None:
    """An injected pool is the one read: the live snapshot reader is not consulted, and its absence is not an error."""
    out, code, _store = tick_with(tmp_path, record_text=_fresh_record(), **all_bound())
    assert "pool-unread" not in out
    assert code == OK


def test_seam_brake_held_by_the_injected_brake_names_the_hold(tmp_path: Path) -> None:
    """A brake that answers True holds the pass: it starts nothing and the pass names the open-count hold."""
    bindings = {**all_bound(), "brake": lambda: True}
    out, _code, store = tick_with(tmp_path, record_text=_fresh_record(), **bindings)
    assert "open-count" in out
    assert store.load(OVERLAY["scope"]).version == 0 or "started" not in out


def test_seam_stop_signal_refusal_names_the_hold(tmp_path: Path) -> None:
    """A stop that refuses holds the pass and the refusal text is printed in the hold."""
    bindings = {**all_bound(), "stop": lambda _c: "stop-text"}
    out, _code, _store = tick_with(tmp_path, record_text=_fresh_record(), **bindings)
    assert "stop-signal:stop-text" in out


def test_seam_ticket_mint_is_unavailable_when_unbound() -> None:
    """The unbound tickets stand-in answers the mint as ``unavailable`` naming the cause, and carries no start criterion."""
    result = UnboundFloorTickets().mint("k")
    assert result.state == "unavailable"
    assert result.cause == "unbound:tickets"
    assert UnboundFloorTickets().start_criteria("{}") == ()


def test_seam_reader_floor_record_with_needs_cell_still_reads(tmp_path: Path) -> None:
    """The reader lane adds ``needs`` to ``ModelRecordStopped``; the record model ignores unknown cells, so both shapes read.

    A fresh record carrying a stopped entry with a ``needs`` cell still reads (no ``floor-unread``) and the tick ends OK.
    """
    record = json.loads(_fresh_record())
    record["floor"]["stopped"] = [
        {"lane": "x-1", "outcome": "partial", "needs": "decision:ask"}
    ]
    out, code, _store = tick_with(
        tmp_path, record_text=json.dumps(record), **all_bound()
    )
    assert "floor-unread" not in out
    assert code == OK
