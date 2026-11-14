# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor counts the handed-off pull requests the landing controller refuses, and the ones nobody was told of (OMN-23242).

On 2026-11-08 the controller wrote 181 CRITERIA_DRIFT merge deferrals and kept 52 pull requests out; pull request #1753 of the plugin repository was
deferred 73 times between 01:05Z and 23:55Z before any brief named the drift. The controller now writes one MSG to the hand-off
lane's parent per pull request, head and gate (the plugin repository's #1796); the floor shows how many handed-off pull requests are
deferred or kept out and how many of the deferred ones lasted more than 3 ticks with no such MSG.

The rows are the shapes the controller writes (its ``decisions.jsonl`` merge row and ``admissions.jsonl`` kept_out row, the
hand-off and parent MSG rows of its own tests), laid out as the 2026-11-08 case: the controller's journals live on its host and
are not readable from the lab, so the 73 deferrals of #1753 are regenerated at the recorded span and gate, not copied.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import ModelFloorSimple, ModelFloorSimpleTickRequest
from omnimarket.models.floor_17a.model_floor_handoff_refusals import (
    ModelHandoffRefusalsOverlay,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handoff_refusals_section import (
    build_handoff_refusals_section,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handoff_refusals import (
    deferral_rows,
    handoff_ledger_facts,
    handoff_refusals_reading,
    kept_out_rows,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_floor_simple_effect.support import build_home, environ

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 23, 58, tzinfo=UTC)
OVERLAY = ModelHandoffRefusalsOverlay()
H = "a" * 40
H2 = "b" * 40
DRIFT = "OMN-19998: CRITERIA_DRIFT: AC20 deleted from the ticket"


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def handoff_msg(at: datetime, lane: str, pr: str, head: str = H) -> str:
    return (
        f"{stamp(at)} | MSG | from={lane} | to=landing-controller | id={stamp(at)}-{lane} | ticket=OMN-22478 | source=title | "
        f"repo={pr.split('#')[0]} | pr={pr} | head={head} | needs=land | Handoff from {lane}: {pr} is ready to land at this head."
    )


def parent_msg(at: datetime, pr: str, gate: str, head: str = H) -> str:
    return (
        f"{stamp(at)} | MSG | from=landing-controller | id={stamp(at)}-landing-controller | to=orchestrator-31d9 | ticket=OMN-19998 | "
        f"pr={pr} | head={head} | gate={gate} | handoff-lane=lane-x | kind=gate | {pr} at head {head} is held at gate {gate}"
    )


# The 2026-11-08 case, as the ledger and the controller's journals would read at 23:58Z.
A = "omniclaude#1753"  # 73 CRITERIA_DRIFT deferrals, no parent MSG: the silent one
B = "omnimarket#4000"  # 8 deferrals, the parent was told at this head and gate
C = "omnibase_infra#5000"  # 2 deferrals: not yet past 3 ticks
D = "omnibase_core#3000"  # kept out by the goal admission, handed off
E = "omnimarket#4001"  # 10 deferrals but never handed off to the controller
F = "omnimarket#4002"  # 20 deferrals at the strict base's queue order: nothing for a parent to act on
G = "omnimarket#4003"  # deferred 5 ticks, then merged: the deferral ended


def ledger_lines() -> list[str]:
    t = NOW - timedelta(hours=23)
    return [
        handoff_msg(t, "lane-handoff-0100", A),
        handoff_msg(t, "lane-b", B),
        handoff_msg(t, "lane-c", C),
        handoff_msg(t, "lane-d", D),
        handoff_msg(t, "lane-f", F),
        handoff_msg(t, "lane-g", G),
        parent_msg(NOW - timedelta(hours=2), B, "CRITERIA_DRIFT"),
        # a parent MSG for another head of A does not tell its parent of the head that is deferred now
        parent_msg(NOW - timedelta(hours=20), A, "CRITERIA_DRIFT", head=H2),
        "2026-11-08T01:05:00Z | MSG | from=lane-other-0100 | to=orchestrator-31d9 | ticket=OMN-22478 | pr=omnimarket#9 | an unrelated message",
    ]


def merge_row(
    pr: str, tick: int, at: datetime, outcome: str = "deferred", **over: Any
) -> dict[str, Any]:
    return {
        "ts": stamp(at),
        "mode": "act",
        "tick": tick,
        "kind": "merge",
        "subject": f"OmniNode-ai/{pr}",
        "head_sha": H,
        "outcome": outcome,
        "detail": DRIFT,
        **over,
    }


def decisions() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    first = datetime(2026, 11, 8, 1, 5, tzinfo=UTC)
    last = datetime(2026, 11, 8, 23, 55, tzinfo=UTC)
    step = (last - first) / 72
    out += [merge_row(A, 100 + n, first + step * n) for n in range(73)]
    out += [
        merge_row(B, 500 + n, NOW - timedelta(minutes=5 * (8 - n))) for n in range(8)
    ]
    out += [
        merge_row(C, 600 + n, NOW - timedelta(minutes=5 * (2 - n))) for n in range(2)
    ]
    out += [
        merge_row(E, 700 + n, NOW - timedelta(minutes=5 * (10 - n))) for n in range(10)
    ]
    out += [
        merge_row(
            F,
            800 + n,
            NOW - timedelta(minutes=5 * (20 - n)),
            detail="strict base: queue order",
        )
        for n in range(20)
    ]
    out += [
        merge_row(G, 900 + n, NOW - timedelta(minutes=30 - 5 * n)) for n in range(5)
    ]
    out += [merge_row(G, 905, NOW - timedelta(minutes=2), outcome="merged", detail="")]
    # a pipeline tick-budget deferral is not a refusal
    out += [merge_row(C, 650, NOW - timedelta(minutes=1), pipeline=True)]
    return out


def admissions() -> dict[str, dict[str, Any]]:
    return {
        D: {
            "kind": "admission",
            "event": "kept_out",
            "pr": D,
            "head": H,
            "admitted": False,
            "ts": stamp(NOW),
        },
        "omnimarket#1": {
            "kind": "admission",
            "event": "admitted",
            "pr": "omnimarket#1",
            "head": H,
            "admitted": True,
        },
    }


def facts(lines: list[str] | None = None) -> Any:
    rows = [
        evidence_row_from_text(x) for x in (ledger_lines() if lines is None else lines)
    ]
    return handoff_ledger_facts(
        [r for r in rows if r is not None], "landing-controller"
    )


def reading(
    lines: list[str] | None = None, rows: list[dict[str, Any]] | None = None
) -> Any:
    return handoff_refusals_reading(
        ledger=facts(lines),
        ledger_error="",
        decisions=decisions() if rows is None else rows,
        admissions=admissions(),
        now=NOW,
        overlay=OVERLAY,
    )


def test_golden_the_2026_11_08_case_counts_the_four_refused_and_the_one_silent() -> (
    None
):
    section = build_handoff_refusals_section(OVERLAY, reading())
    assert section.status == "read"
    # A, B and C are deferred, D is kept out; E was never handed off, F is the queue order, G merged.
    assert (section.refused, section.deferred, section.kept_out) == (4, 3, 1)
    # Of the deferred ones only A lasted more than 3 ticks with no parent MSG at its head and gate: B was told, C is at 2 ticks.
    assert (section.silent, section.silent_prs) == (1, (A,))
    by_pr = {line.pr: line for line in section.lines}
    assert by_pr[A].ticks == 73
    assert by_pr[A].parent_told is False
    assert by_pr[A].handoff_lane == "lane-handoff-0100"
    assert by_pr[B].ticks == 8
    assert by_pr[B].parent_told is True
    assert by_pr[C].ticks == 2
    assert by_pr[D].kind == "kept_out"
    assert by_pr[D].gate == "KEPT_OUT"
    assert section.text == (
        "4 handed-off pull requests deferred or kept out; 1 deferred more than 3 ticks with no parent MSG"
    )


def test_a_parent_msg_for_the_deferred_head_and_gate_clears_the_silent_count() -> None:
    told = [
        *ledger_lines(),
        parent_msg(NOW - timedelta(minutes=1), A, "CRITERIA_DRIFT"),
    ]
    section = build_handoff_refusals_section(OVERLAY, reading(told))
    assert (section.refused, section.silent) == (4, 0)


def test_a_parent_msg_of_another_gate_does_not_clear_it() -> None:
    told = [
        *ledger_lines(),
        parent_msg(NOW - timedelta(minutes=1), A, "RELEASE_PREMISE"),
    ]
    assert build_handoff_refusals_section(OVERLAY, reading(told)).silent == 1


def test_the_fourth_tick_is_the_first_silent_one() -> None:
    def silent(ticks: int) -> int:
        rows = [
            merge_row(A, n, NOW - timedelta(minutes=5 * (ticks - n)))
            for n in range(ticks)
        ]
        return build_handoff_refusals_section(OVERLAY, reading(rows=rows)).silent

    assert (silent(3), silent(4)) == (0, 1)


def test_a_deferral_that_stopped_being_written_is_no_longer_counted() -> None:
    old = [merge_row(A, n, NOW - timedelta(hours=3, minutes=5 * n)) for n in range(10)]
    section = build_handoff_refusals_section(OVERLAY, reading(rows=old))
    assert (section.refused, section.silent) == (1, 0)  # only D, kept out


def test_the_failing_control_without_the_parent_msg_reader_nothing_is_told_of() -> None:
    """Without the ledger's parent MSG rows every deferred pull request past 3 ticks reads silent, B included."""
    only_handoffs = [x for x in ledger_lines() if "from=landing-controller" not in x]
    section = build_handoff_refusals_section(OVERLAY, reading(only_handoffs))
    assert section.silent == 2


def test_error_an_unread_source_is_drawn_unread_with_its_reason_never_zero() -> None:
    for bad, reason in (
        (
            handoff_refusals_reading(
                ledger=None,
                ledger_error="MSG rows unread: boom",
                decisions=[],
                admissions={},
                now=NOW,
                overlay=OVERLAY,
            ),
            "ledger hand-off rows unread: MSG rows unread: boom",
        ),
        (
            handoff_refusals_reading(
                ledger=facts(),
                ledger_error="",
                decisions=None,
                admissions={},
                now=NOW,
                overlay=OVERLAY,
            ),
            "the controller's decisions.jsonl was not readable",
        ),
        (
            handoff_refusals_reading(
                ledger=facts(),
                ledger_error="",
                decisions=[],
                admissions=None,
                now=NOW,
                overlay=OVERLAY,
            ),
            "the controller's admissions.jsonl was not readable",
        ),
    ):
        section = build_handoff_refusals_section(OVERLAY, bad)
        assert section.status == "unread"
        assert section.reason == reason
        assert (section.refused, section.silent) == (0, 0)
    assert build_handoff_refusals_section(OVERLAY, None).status == "unread"


def test_the_kept_out_rows_are_the_newest_admission_rows_that_keep_a_pull_request_out() -> (
    None
):
    assert [(r.pr, r.gate) for r in kept_out_rows(admissions())] == [(D, "KEPT_OUT")]
    assert [r.pr for r in deferral_rows(decisions(), NOW, OVERLAY)] == sorted(
        [A, B, C, E]
    )


def write_journals(home: Path) -> None:
    landing = home / "landing"
    landing.mkdir(parents=True, exist_ok=True)
    (landing / "decisions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in decisions()) + "not json\n",
        encoding="utf-8",
    )
    (landing / "admissions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in admissions().values()), encoding="utf-8"
    )


def run_cycle(home: Path, ledger: InMemoryLedger) -> ModelFloorSimple:
    result = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: NOW,
        served_alarm_ids=lambda: None,
        ledger_source=ledger,
    ).run(ModelFloorSimpleTickRequest(home=str(home), now=NOW), environ=environ(home))
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


def test_golden_the_served_floor_carries_both_counts_after_a_cycle(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    write_journals(home)
    section = run_cycle(home, InMemoryLedger(ledger_lines())).handoff_refusals
    assert section is not None
    assert section.status == "read"
    assert (section.refused, section.silent) == (4, 1)
    assert section.silent_prs == (A,)


def test_error_a_cycle_without_the_controller_journals_draws_the_section_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    section = run_cycle(home, InMemoryLedger(ledger_lines())).handoff_refusals
    assert section is not None
    assert section.status == "unread"
    assert "decisions.jsonl" in section.reason
    assert (section.refused, section.silent) == (0, 0)


def test_error_a_cycle_whose_ledger_cannot_be_read_draws_the_section_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    write_journals(home)
    ledger = InMemoryLedger(ledger_lines())
    ledger.read_error = "ledger down"
    section = run_cycle(home, ledger).handoff_refusals
    assert section is not None
    assert section.status == "unread"
    assert "ledger" in section.reason
