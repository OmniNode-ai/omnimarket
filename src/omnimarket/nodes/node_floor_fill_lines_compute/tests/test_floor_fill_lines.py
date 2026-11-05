# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill lines compute names each untouched startable line (plan T4).

The fixtures are built directly against the request model, so these tests pass before the
orchestrator that builds the same request at pass step S6 exists. The printed lines are
pinned in full, the way the tick prints them.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_lines_compute.handlers.handler_floor_fill_lines import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_lines_compute.models import (
    EnumFloorFillLinesLevel,
    EnumFloorFillLinesSource,
    ModelFloorFillLinesRequest,
    ModelFloorFillLinesResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
HANDLER_SOURCE = NODE_DIR / "handlers" / "handler_floor_fill_lines.py"
T0 = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
LINES = ("alpha", "bravo", "charlie", "delta")


def startable(line_id: str, *, ticket: str | None) -> dict[str, Any]:
    return {
        "line_id": line_id,
        "verdict": "STARTABLE" if ticket else "NEEDS-TICKET",
        "next_task_id": f"{line_id}-t1",
        "ticket": ticket,
    }


def request(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unread": [],
        "lines": [startable(line, ticket=f"T-{line}") for line in LINES],
        "entries": [],
        "now": T0.isoformat(),
        "reserved_timeout_seconds": 600,
        "unresolved_keys": [],
        "start_results": [],
        "free_slots_after": 20,
        "hold": "none",
    }
    return base | fields


def handle(**fields: Any) -> ModelFloorFillLinesResult:
    return HandlerFloorFillLines().handle(
        ModelFloorFillLinesRequest.model_validate(request(**fields))
    )


def failed(cause: str = "runner-exit-3") -> list[dict[str, Any]]:
    return [
        {"key": f"floor:{line}:{line}-t1", "kind": "failed", "cause": cause}
        for line in LINES
    ]


def started() -> list[dict[str, Any]]:
    return [
        {
            "key": f"floor:{line}:{line}-t1",
            "kind": "started",
            "lane": f"floor-{line}-a1",
        }
        for line in LINES
    ]


def only(out: ModelFloorFillLinesResult, prefix: str) -> list[str]:
    return [line for line in out.rendered() if line.startswith(prefix)]


# ------------------------------------------------------------------ AC1


def test_four_startable_lines_with_the_runner_failing_print_four_stopped_lines() -> (
    None
):
    out = handle(start_results=failed())
    assert out.rendered() == (
        "MISSING line-stopped:alpha | next=alpha-t1 ticket=T-alpha slots_free=20 cause=start-failed:runner-exit-3",
        "MISSING line-stopped:bravo | next=bravo-t1 ticket=T-bravo slots_free=20 cause=start-failed:runner-exit-3",
        "MISSING line-stopped:charlie | next=charlie-t1 ticket=T-charlie slots_free=20 cause=start-failed:runner-exit-3",
        "MISSING line-stopped:delta | next=delta-t1 ticket=T-delta slots_free=20 cause=start-failed:runner-exit-3",
    )
    assert all(f.level is EnumFloorFillLinesLevel.MISSING for f in out.findings)
    assert all(f.fix for f in out.findings)


def test_a_refused_mint_names_its_own_cause() -> None:
    lines = [startable("alpha", ticket=None)]
    results = [
        {"key": "floor:alpha:alpha-t1", "kind": "mint-refused", "cause": "gate-refused"}
    ]
    out = handle(lines=lines, start_results=results)
    assert out.rendered() == (
        "MISSING line-stopped:alpha | next=alpha-t1 ticket=none slots_free=20 cause=mint-refused:gate-refused",
    )


def test_a_line_no_start_was_tried_for_has_no_cause_cell() -> None:
    out = handle()
    assert out.rendered()[0] == (
        "MISSING line-stopped:alpha | next=alpha-t1 ticket=T-alpha slots_free=20"
    )
    assert len(only(out, "MISSING line-stopped:")) == 4


def test_the_cause_of_the_next_task_wins_over_another_task_of_the_line() -> None:
    results = [
        {"key": "floor:alpha:alpha-t0", "kind": "failed", "cause": "other"},
        {"key": "floor:alpha:alpha-t1", "kind": "failed", "cause": "next"},
    ]
    out = handle(lines=[startable("alpha", ticket="T-1")], start_results=results)
    assert out.rendered() == (
        "MISSING line-stopped:alpha | next=alpha-t1 ticket=T-1 slots_free=20 cause=start-failed:next",
    )


@pytest.mark.parametrize("source", [s.value for s in EnumFloorFillLinesSource])
def test_unread_sources_rendered(source: str) -> None:
    out = handle(unread=[f"{source}:read timed out"], start_results=failed())
    assert out.rendered() == (f"UNKNOWN {source}-unread:read timed out",)
    [finding] = out.findings
    assert finding.level is EnumFloorFillLinesLevel.UNKNOWN
    assert finding.fix


def test_unread_sources_rendered_each_once_and_nothing_else() -> None:
    out = handle(
        unread=["pool:no host", "floor:stale", "pool:again"],
        entries=[
            {
                "key": "floor:alpha:alpha-t1",
                "line_id": "alpha",
                "state": "RESERVED",
                "lane": "x",
                "reserved_at": (T0 - timedelta(hours=2)).isoformat(),
            }
        ],
        unresolved_keys=["floor:alpha:alpha-t1"],
    )
    assert out.rendered() == (
        "UNKNOWN pool-unread:no host",
        "UNKNOWN floor-unread:stale",
    )


@pytest.mark.parametrize("bad", ["floor", "floor:", "disk:full", ":cause", "pool: "])
def test_an_unread_item_must_be_a_source_and_a_cause(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(request(unread=[bad]))


# ------------------------------------------------------------------ AC2


def test_with_the_runner_working_four_lines_are_filled() -> None:
    out = handle(start_results=started())
    assert out.rendered() == (
        "NOTE line-filled:alpha | lane=floor-alpha-a1",
        "NOTE line-filled:bravo | lane=floor-bravo-a1",
        "NOTE line-filled:charlie | lane=floor-charlie-a1",
        "NOTE line-filled:delta | lane=floor-delta-a1",
    )
    assert not only(out, "MISSING line-stopped:")
    assert all(f.fix == "" for f in out.findings)


def test_two_lanes_started_on_one_line_print_one_filled_line() -> None:
    results = [
        {"key": "floor:alpha:alpha-t1", "kind": "started", "lane": "floor-a-a1"},
        {"key": "floor:alpha:alpha-t2", "kind": "started", "lane": "floor-b-a1"},
    ]
    out = handle(lines=[startable("alpha", ticket="T-1")], start_results=results)
    assert out.rendered() == ("NOTE line-filled:alpha | lane=floor-a-a1,floor-b-a1",)


def test_with_no_free_slots_each_startable_line_is_held() -> None:
    out = handle(free_slots_after=0)
    assert out.rendered() == tuple(
        f"NOTE line-held:{line} | no-free-slots" for line in LINES
    )
    assert not only(out, "MISSING")


def test_a_started_line_is_filled_even_when_it_used_the_last_slot() -> None:
    out = handle(
        free_slots_after=0,
        lines=[startable("alpha", ticket="T-1"), startable("bravo", ticket="T-2")],
        start_results=[
            {"key": "floor:alpha:alpha-t1", "kind": "started", "lane": "floor-a-a1"}
        ],
    )
    assert out.rendered() == (
        "NOTE line-filled:alpha | lane=floor-a-a1",
        "NOTE line-held:bravo | no-free-slots",
    )


# ------------------------------------------------------------------ AC3


def test_the_open_count_brake_holds_every_startable_line() -> None:
    out = handle(hold="open-count")
    assert out.rendered() == tuple(
        f"NOTE line-held:{line} | open-count" for line in LINES
    )
    assert not only(out, "MISSING")


def test_a_stop_signal_holds_every_startable_line_with_its_cause() -> None:
    out = handle(hold="stop-signal:operator-stop")
    assert out.rendered() == tuple(
        f"NOTE line-held:{line} | stop-signal:operator-stop" for line in LINES
    )
    assert not only(out, "MISSING")


@pytest.mark.parametrize("hold", ["open-count", "stop-signal:x"])
def test_a_hold_beats_the_slot_count_and_never_prints_stopped(hold: str) -> None:
    for slots in (0, 1, 20):
        out = handle(hold=hold, free_slots_after=slots)
        assert only(out, "MISSING") == []
        assert len(only(out, "NOTE line-held:")) == 4
        assert all(line.endswith(f"| {hold}") for line in out.rendered())


@pytest.mark.parametrize("hold", ["", "brake", "stop-signal:", "open-count extra"])
def test_a_hold_that_is_not_a_hold_is_refused(hold: str) -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(request(hold=hold))


# ------------------------------------------------------------------ AC4


UNTICKETABLE_CAUSES = (
    "needs-spec:acceptance,gate",
    "no-parent",
    "ticket-refused:umbrella",
    "ticket-refused:old-fill-visible",
    "ticket-unreadable",
    "plan-unread",
)


@pytest.mark.parametrize("cause", UNTICKETABLE_CAUSES)
def test_line_ticket_causes(cause: str) -> None:
    line = {
        "line_id": "echo",
        "verdict": "UNTICKETABLE",
        "untickable": [{"task_id": "echo-t9", "cause": cause}],
    }
    out = handle(lines=[line], free_slots_after=0, hold="open-count")
    assert out.rendered() == (f"MISSING line-ticket:echo | task=echo-t9 cause={cause}",)
    assert out.findings[0].fix


def test_line_ticket_causes_one_line_per_untickable_task() -> None:
    line = {
        "line_id": "echo",
        "verdict": "UNTICKETABLE",
        "untickable": [
            {"task_id": "echo-t1", "cause": "no-parent"},
            {"task_id": "echo-t2", "cause": "plan-unread"},
        ],
    }
    assert handle(lines=[line]).rendered() == (
        "MISSING line-ticket:echo | task=echo-t1 cause=no-parent",
        "MISSING line-ticket:echo | task=echo-t2 cause=plan-unread",
    )


def test_an_untickable_task_beside_a_startable_one_prints_no_ticket_line() -> None:
    line = startable("echo", ticket="T-1") | {
        "untickable": [{"task_id": "echo-t0", "cause": "no-parent"}]
    }
    out = handle(lines=[line])
    assert only(out, "MISSING line-ticket:") == []
    assert only(out, "MISSING line-stopped:") != []


def _reserved_entry(minutes_ago: float, **fields: Any) -> dict[str, Any]:
    return {
        "key": "floor:alpha:alpha-t1",
        "line_id": "alpha",
        "state": "RESERVED",
        "lane": "floor-alpha-a1",
        "reserved_at": (T0 - timedelta(minutes=minutes_ago)).isoformat(),
    } | fields


def test_line_reserved_unresolved() -> None:
    out = handle(
        lines=[],
        entries=[_reserved_entry(17.9)],
        unresolved_keys=["floor:alpha:alpha-t1"],
    )
    assert out.rendered() == (
        "MISSING line-reserved:floor:alpha:alpha-t1 | line=alpha age=17m",
    )
    assert out.findings[0].fix


def test_line_reserved_unresolved_is_silent_before_the_timeout() -> None:
    entries = [_reserved_entry(9.9)]
    out = handle(lines=[], entries=entries, unresolved_keys=["floor:alpha:alpha-t1"])
    assert out.rendered() == ()


def test_line_reserved_unresolved_prints_exactly_at_the_timeout() -> None:
    out = handle(
        lines=[],
        entries=[_reserved_entry(10)],
        unresolved_keys=["floor:alpha:alpha-t1"],
    )
    assert out.rendered() == (
        "MISSING line-reserved:floor:alpha:alpha-t1 | line=alpha age=10m",
    )


def test_line_reserved_unresolved_needs_the_ledger_read_to_have_failed() -> None:
    out = handle(lines=[], entries=[_reserved_entry(120)], unresolved_keys=[])
    assert out.rendered() == ()


def test_line_reserved_unresolved_ignores_a_sent_entry() -> None:
    out = handle(
        lines=[],
        entries=[_reserved_entry(120, state="SENT")],
        unresolved_keys=["floor:alpha:alpha-t1"],
    )
    assert out.rendered() == ()


def test_an_unresolved_key_that_names_no_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(
            request(unresolved_keys=["floor:alpha:alpha-t1"])
        )


# ------------------------------------------------------------------ the NOTE kinds


def test_at_capacity_and_no_startable_lines_are_notes_never_stopped() -> None:
    lines = [
        {"line_id": "full", "verdict": "AT-CAPACITY"},
        {"line_id": "idle", "verdict": "NO-STARTABLE", "cause": "wait:T0.1"},
    ]
    out = handle(lines=lines)
    assert out.rendered() == (
        "NOTE line-at-capacity:full",
        "NOTE line-blocked:idle | wait:T0.1",
    )
    assert only(out, "MISSING") == []


def test_every_line_is_named_once_in_input_order() -> None:
    lines = [
        {"line_id": "n", "verdict": "NO-STARTABLE", "cause": "done"},
        startable("s", ticket="T-1"),
        {"line_id": "c", "verdict": "AT-CAPACITY"},
    ]
    out = handle(lines=lines)
    assert [f.loop for f in out.findings] == [
        "line-blocked:n",
        "line-stopped:s",
        "line-at-capacity:c",
    ]


def test_the_lines_come_in_the_ticks_finding_tuple_shape() -> None:
    out = handle(start_results=failed())
    level, loop, why, fix = out.findings[0].as_tuple()
    assert (level, loop) == ("missing", "line-stopped:alpha")
    assert why.startswith("next=alpha-t1 ")
    assert fix


def test_the_same_request_gives_byte_identical_output() -> None:
    first = handle(start_results=failed()).model_dump_json()
    assert handle(start_results=failed()).model_dump_json() == first


@pytest.mark.parametrize(
    "line",
    [
        {"line_id": "a", "verdict": "STARTABLE"},
        {"line_id": "a", "verdict": "NEEDS-TICKET", "next_task_id": "t", "ticket": "T"},
        {"line_id": "a", "verdict": "UNTICKETABLE"},
        {"line_id": "a", "verdict": "NO-STARTABLE"},
        {"line_id": "a:b", "verdict": "AT-CAPACITY"},
    ],
)
def test_a_line_verdict_without_its_cells_is_refused(line: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(request(lines=[line]))


@pytest.mark.parametrize(
    "result",
    [
        {"key": "floor:a:t", "kind": "started"},
        {"key": "floor:a:t", "kind": "failed"},
        {"key": "floor:a:t", "kind": "mint-refused", "cause": ""},
        {"key": "not-a-key", "kind": "failed", "cause": "x"},
    ],
)
def test_a_start_result_without_its_cell_is_refused(result: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(request(start_results=[result]))


# ------------------------------------------------------------------ G-NODE


def test_config_variation_free_slots_follow_the_request() -> None:
    held = handle(free_slots_after=0)
    open_ = handle(free_slots_after=3)
    assert only(held, "NOTE line-held:")
    assert not only(held, "MISSING")
    assert only(open_, "MISSING line-stopped:")
    assert not only(open_, "NOTE")
    assert all("slots_free=3" in line for line in open_.rendered())


def test_config_variation_reserved_timeout_follows_the_request() -> None:
    for timeout, printed in ((300, True), (1200, False)):
        out = handle(
            lines=[],
            entries=[_reserved_entry(10)],
            unresolved_keys=["floor:alpha:alpha-t1"],
            reserved_timeout_seconds=timeout,
        )
        assert bool(out.rendered()) is printed


def test_missing_key_refused_naming_the_key() -> None:
    valid = request()
    ModelFloorFillLinesRequest.model_validate(valid)
    for field in ("now", "reserved_timeout_seconds", "free_slots_after", "hold"):
        partial = {k: v for k, v in valid.items() if k != field}
        with pytest.raises(ValidationError) as refused:
            ModelFloorFillLinesRequest.model_validate(partial)
        assert field in str(refused.value), field


def test_a_naive_clock_reading_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFloorFillLinesRequest.model_validate(request(now="2026-11-03T04:00:00"))


def test_seams_in_spi_the_node_injects_no_protocol() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert not contract.get("protocol_dependencies")
    assert HandlerFloorFillLines().__dict__ == {}
    tree = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
    [handler] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "HandlerFloorFillLines"
    ]
    assert not [
        f
        for f in ast.walk(handler)
        if isinstance(f, ast.FunctionDef) and f.name == "__init__"
    ]


def test_the_node_reads_no_clock_and_does_no_io() -> None:
    tree = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
    forbidden = {
        "os",
        "sys",
        "time",
        "datetime",
        "socket",
        "subprocess",
        "pathlib",
        "random",
        "uuid",
        "requests",
        "httpx",
        "asyncio",
        "logging",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & forbidden
    attrs = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not attrs & {"now", "utcnow", "today", "time", "read_text", "write_text"}
    names = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not names & {"open", "print"}


def test_the_node_imports_no_other_nodes_models() -> None:
    for path in NODE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("omnimarket.nodes.")
            ):
                assert node.module.startswith(
                    "omnimarket.nodes.node_floor_fill_lines_compute"
                ), (path.name, node.module)


def test_the_contract_loads_and_names_the_node() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "node_floor_fill_lines_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["input_model"]["name"] == "ModelFloorFillLinesRequest"
    assert contract["output_model"]["name"] == "ModelFloorFillLinesResult"
    assert contract["descriptor"]["purity"] == "pure"
    assert not contract["event_bus"]["subscribe_topics"]
    assert not contract["event_bus"]["publish_topics"]
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_floor_fill_lines_compute"
    assert metadata["node_role"] == "compute"
