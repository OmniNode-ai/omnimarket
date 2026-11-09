# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor names the lane that owns a holdup from an ``owns=floor:<holdup id>`` cell (OMN-22766).

Before this change a lane matched a holdup only by its ticket or its pull request, so every stoppage class, chain and
piece holdup printed "Nobody is on it" however many lanes were on it: the CLAIM ``cause=`` cells key the red checks of
pull requests, not the floor's holdup ids. A lane now owns a holdup by an explicit ``owns=floor:<holdup id>`` cell on
its CLAIM or a later STATUS row (a comma list is allowed), the rule the ledger MSG uses for pull requests.

The rows below carry the cells of today's ledger rows (the principal cell scrubbed) and run through the ledger parser,
the factory floor's lane builder, the floor record's JSON and the simple floor, so the read is the one the served page
makes. The failing control is the run of the same rows against the code before the change (the test commit alone).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import parse_ledger_text
from omnimarket.nodes.node_floor_simple_compute.handlers import (
    HandlerFloorSimple,
    check_red_rule,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import record_lanes
from tests.nodes.node_factory_floor_compute.support import NOW as LEDGER_NOW
from tests.nodes.node_factory_floor_compute.support import config, request
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

STALL = config().stall_minutes


def stamp(minutes_ago: float) -> str:
    return (LEDGER_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim(lane: str, ago: float, cells: str = "") -> str:
    return (
        f"{stamp(ago)} | CLAIM | lane={lane} | principal=dev-recorded | ticket=OMN-22478 | actor=claude | "
        f"model=sonnet | worktree=none | est ~4 lane-hours; displaces nobody; (OMN-22478) | "
        f"parent=orchestrator-31d9{cells} | via=bus:h200 | a lane on the floor"
    )


def status(lane: str, ago: float, cells: str = "") -> str:
    return (
        f"{stamp(ago)} | STATUS | lane={lane} | principal=dev-recorded | ticket=OMN-22478 | actor=claude | "
        f"model=sonnet | parent=orchestrator-31d9{cells} | via=bus:h200 | working on it"
    )


def terminal(lane: str, ago: float) -> str:
    return (
        f"{stamp(ago)} | TERMINAL | lane={lane} | principal=dev-recorded | ticket=OMN-22478 | actor=claude | "
        f"model=sonnet | outcome=done | via=bus:h200 | done"
    )


def ledger(*rows: str) -> str:
    return "\n".join(f"L{n} {text}" for n, text in enumerate(rows, start=1))


# Six lanes, each in a state the floor has to tell apart:
#   owner-claim   a live lane that owns a stoppage by its CLAIM
#   owner-status  a live lane that owns two holdups by a later STATUS row (a comma list)
#   owner-done    wrote the cell, then ended with a TERMINAL: it is on nothing
#   owner-stalled wrote the cell, then went silent past the stall bound: it is on nothing
#   owner-typo    owns an id the floor does not print: it is on nothing the floor shows
#   no-cell       a live lane with the umbrella ticket and no owns cell: it is on nothing
ROWS = ledger(
    claim("owner-claim", 30, " | owns=floor:stoppage:lab-idle"),
    claim("owner-status", 40),
    status("owner-status", 5, " | owns=floor:stoppage:job-exit,floor:prs:red"),
    claim("owner-done", 60, " | owns=floor:stoppage:throughput-results"),
    terminal("owner-done", 10),
    claim("owner-stalled", STALL + 5, " | owns=floor:stoppage:quiet-host"),
    claim("owner-typo", 20, " | owns=floor:stoppage:no-such-class"),
    claim("no-cell", 15),
)

STOPPAGES = tuple(
    ModelFloorCauseIn(
        id=f"stoppage:{name}",
        title=name,
        detail="seen",
        size=size,
        size_unit="occurrences",
    )
    for name, size in (
        ("lab-idle", 9),
        ("job-exit", 8),
        ("throughput-results", 7),
        ("quiet-host", 6),
    )
)


def served_lanes(text: str) -> tuple[ModelFloorLane, ...]:
    """The lanes as the served record draws them: the factory floor's lane list, as JSON, read back by the simple floor."""
    floor = compute_factory_floor(request(ledger=parse_ledger_text(text))).floor
    return record_lanes({"floor": floor.model_dump(mode="json")})


def floor_doc(lanes: tuple[ModelFloorLane, ...]) -> ModelFloorSimple:
    overlay = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=LEDGER_NOW,
            overlay=overlay,
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=LEDGER_NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=LEDGER_NOW - timedelta(minutes=1),
            pr_class_counts={"red": 2},
            pr_class_tickets={"red": ("OMN-1",)},
            pr_class_prs={"red": ("r#1", "r#2")},
            stoppages=STOPPAGES,
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
        )
    )


def cause(doc: ModelFloorSimple, cause_id: str) -> tuple[str, tuple[str, ...]]:
    found = next(c for c in doc.causes if c.id == cause_id)
    return found.state, found.lanes


def test_golden_a_live_lane_owns_a_holdup_by_its_claim_or_a_status_row() -> None:
    lanes = served_lanes(ROWS)
    doc = floor_doc(lanes)
    assert cause(doc, "stoppage:lab-idle") == ("amber", ("owner-claim",))
    assert cause(doc, "stoppage:job-exit") == ("amber", ("owner-status",))
    # The comma list gives the lane both holdups, and a holdup a lane owns keeps its pull request reading too.
    assert cause(doc, "prs:red") == ("amber", ("owner-status",))
    assert check_red_rule(doc, lanes).violations == ()


def test_error_a_lane_that_ended_went_silent_or_names_no_holdup_is_not_on_one() -> None:
    doc = floor_doc(served_lanes(ROWS))
    # owner-done ended with a TERMINAL, owner-stalled is past the stall bound, owner-typo owns an id nobody prints.
    assert cause(doc, "stoppage:throughput-results") == ("red", ())
    assert cause(doc, "stoppage:quiet-host") == ("red", ())
    assert all(c.id != "stoppage:no-such-class" for c in doc.causes)
    # A live lane with the umbrella ticket and no owns cell is on nothing.
    assert all("no-cell" not in c.lanes for c in doc.causes)


def test_error_a_dead_or_finished_process_reading_is_on_nothing() -> None:
    for liveness in ("dead", "finished"):
        lane = ModelFloorLane(
            lane="gone",
            liveness=liveness,
            owns=("stoppage:lab-idle",),
        )
        assert cause(floor_doc((lane,)), "stoppage:lab-idle") == ("red", ())
    live = ModelFloorLane(lane="here", liveness="alive", owns=("stoppage:lab-idle",))
    assert cause(floor_doc((live,)), "stoppage:lab-idle") == ("amber", ("here",))


def test_error_the_unfixed_reading_leaves_every_owned_holdup_red() -> None:
    # The control the other tests are measured against: the same rows with the owns cells taken away, which is what the
    # floor read before the change, draw every holdup red with nobody on it.
    doc = floor_doc(served_lanes(ROWS.replace("owns=", "ownz=")))
    for cause_id in ("stoppage:lab-idle", "stoppage:job-exit", "prs:red"):
        assert cause(doc, cause_id) == ("red", ())


def test_error_a_row_that_is_not_a_claim_or_status_owns_nothing() -> None:
    msg = (
        f"{stamp(3)} | MSG | from=owner-claim | to=orchestrator-31d9 | owns=floor:stoppage:throughput-results | "
        "a message names it and the lane is still not on it"
    )
    doc = floor_doc(served_lanes(f"{ROWS}\nL99 {msg}"))
    assert cause(doc, "stoppage:throughput-results") == ("red", ())
