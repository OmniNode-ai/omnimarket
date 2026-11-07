# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded-floor falsifier of the stopped-line fill (plan task T8, acceptance criteria 1 and 4).

One replay runs the fill's nodes end to end on recorded inputs: the 10:40Z floor record (variant A: six startable
lines, two stopped pills; variant B, the operator's shape: four startable lines), 20 free Claude slots, a scripted
working and a scripted refusing process effect, the plan texts and the ticket store pinned, and a fixed clock.
The orchestrator runs T1's candidates compute, T12's reducer, T4's lines compute, T11's payload compute and mint
effect and T2's scripted runner; the lines reach a reader through the entry the unit's ``tick`` and the ``lines``
command run.

Two tests read the recorded floor as it is (``recorded`` ids) and state the criterion as the plan writes it. Their
control, ``key-safe`` ids, shows what else holds when the id charset of the reducer's key rule is not in the way.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections import Counter
from datetime import timedelta
from typing import Any

import pytest
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    ModelFloorFillAnswer,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedFacts,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    run_entry,
)
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_reducer.models import (
    EnumFloorFillEntryState,
    ModelFloorFillEntry,
    ModelFloorFillState,
)
from tests.integration.stopped_line_fill import support as fixture
from tests.integration.stopped_line_fill.support import Replay, replay

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
VARIANTS = {"a": 6, "b": 4}  # variant -> startable lines
RUNNERS = ("working", "refusing")


class RecordedFloorRefusedError(AssertionError):
    """The tick raised on the recorded floor: the cause is the first line of the message."""


def tick(run: Replay) -> ModelFloorFillAnswer:
    try:
        return run.orchestrator().run(TICK)
    except ValidationError as error:
        first = str(error).splitlines()[1:3]
        raise RecordedFloorRefusedError(
            "the tick raised on the recorded floor instead of starting its lanes: "
            + " ".join(part.strip() for part in first)
        ) from error


def canonical(run: Replay, answer: ModelFloorFillAnswer) -> str:
    """Decisions, rendered lines, status, exit code, resulting rows and every seam's calls, as one string."""
    return json.dumps(
        {
            "decision": answer.decision_projection(),
            "state_seq": answer.state_seq,
            "observed_at": None
            if answer.observed_at is None
            else answer.observed_at.isoformat(),
            "rows": run.rows(),
            "runner_calls": [
                [
                    name,
                    list(spec.argv),
                    list(spec.environment),
                    spec.working_directory,
                    spec.lane,
                ]
                for name, spec, _ in run.runner.calls
            ],
            "facts_calls": run.facts.calls,
            "ticket_calls": run.tickets.calls,
            "tickets": {
                number: stored.description
                for number, stored in sorted(run.ticket_store.tickets.items())
            },
        },
        sort_keys=True,
        indent=1,
    )


def lines_of(answer: ModelFloorFillAnswer, prefix: str) -> list[str]:
    return [line for line in answer.lines if line.startswith(prefix)]


# --------------------------------------------------------------------------- criterion 1 as the plan writes it


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_recorded_floor_working_runner_fills_one_lane_per_startable_line(
    variant: str,
) -> None:
    run = replay(variant=variant, runner="working")
    answer = tick(run)
    started = [r for r in answer.key_results if r.kind == "started"]
    lines = VARIANTS[variant]
    assert len(started) == lines
    assert len(run.runner.calls) == lines  # the script holds exactly that many starts
    per_line = Counter(r.key.split(":")[1] for r in started)
    assert set(per_line.values()) == {1}  # max_fill_lanes_per_line is 1: one per line
    assert len(lines_of(answer, "NOTE line-filled:")) == lines
    assert lines_of(answer, "MISSING line-stopped:") == []


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_recorded_floor_refusing_runner_prints_one_missing_per_startable_line(
    variant: str,
) -> None:
    run = replay(variant=variant, runner="refusing")
    answer = tick(run)
    lines = VARIANTS[variant]
    assert len(lines_of(answer, "MISSING line-stopped:")) == lines
    assert lines_of(answer, "NOTE line-filled:") == []
    assert [r.kind for r in answer.key_results].count("started") == 0
    assert len(run.runner.calls) == lines


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_recorded_floor_replay_is_deterministic(variant: str, runner: str) -> None:
    """Two replays from identical initial state agree byte for byte on decisions, lines, status and rows."""
    first_run, second_run = (
        replay(variant=variant, runner=runner),
        replay(variant=variant, runner=runner),
    )
    first = canonical(first_run, tick(first_run))
    second = canonical(second_run, tick(second_run))
    assert first == second


# --------------------------------------------------------------------------- the control: key-safe ids


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_key_safe_control_working_runner_starts_one_lane_per_startable_line(
    variant: str,
) -> None:
    run = replay(variant=variant, runner="working", ids="key-safe")
    answer = tick(run)
    started = [r for r in answer.key_results if r.kind == "started"]
    lines = VARIANTS[variant]
    assert len(started) == lines
    assert len(run.runner.calls) == lines
    assert set(Counter(r.key.split(":")[1] for r in started).values()) == {1}
    assert lines_of(answer, "MISSING") == []
    assert answer.status.value == "OK"
    # each start carried the key, the minted ticket and the attempt in its argv, and the CLAIM the leader appends
    for result, (name, spec, _) in zip(started, run.runner.calls, strict=True):
        assert name == "start_detached"
        assert spec.argv[:1] == ("node-entry",)
        assert result.key in spec.argv
        assert result.ticket in spec.argv
        assert run.ledger.claim_found(spec.lane, result.key)
    # every started key holds its own minted ticket; a key is minted once (the store holds one ticket per key line)
    store = run.ticket_store.tickets
    key_lines = [
        next(
            line
            for line in t.description.splitlines()
            if line.startswith("Floor key: ")
        )
        for t in store.values()
    ]
    assert len(set(key_lines)) == len(key_lines) >= lines
    for result in started:
        assert result.ticket in store
        assert (
            f"Floor key: {result.key}" in store[result.ticket].description.splitlines()
        )


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_key_safe_control_refusing_runner_prints_one_missing_per_startable_line(
    variant: str,
) -> None:
    run = replay(variant=variant, runner="refusing", ids="key-safe")
    answer = tick(run)
    lines = VARIANTS[variant]
    missing = lines_of(answer, "MISSING line-stopped:")
    assert len(missing) == lines
    assert all("cause=start-failed:exit-3" in line for line in missing)
    assert [r.kind for r in answer.key_results].count("started") == 0
    assert run.ledger.claims == set()  # a refused start leaves no CLAIM row


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_key_safe_control_replay_is_deterministic(variant: str, runner: str) -> None:
    runs = [replay(variant=variant, runner=runner, ids="key-safe") for _ in range(2)]
    first, second = (canonical(r, tick(r)) for r in runs)
    assert first == second


def test_key_safe_control_variant_b_leaves_the_two_finished_lines_untouched() -> None:
    run = replay(variant="b", runner="working", ids="key-safe")
    answer = tick(run)
    blocked = lines_of(answer, "NOTE line-blocked:")
    assert [line.split(" | ")[0] for line in blocked] == [
        "NOTE line-blocked:2026-10-05-line-e-plan",
        "NOTE line-blocked:2026-10-06-line-f-plan",
    ]


def test_the_two_variants_differ_only_in_the_edited_lines() -> None:
    """Variant B is variant A with every prep task of the last two lines marked done, and nothing else."""
    a, b = (json.loads(fixture.floor_text(v)) for v in ("a", "b"))
    assert [line["line_id"] for line in a["lines"]] == [
        line["line_id"] for line in b["lines"]
    ]
    changed = []
    for line_a, line_b in zip(a["lines"], b["lines"], strict=True):
        items_a = [i for s in line_a["stations"] for i in s["items"]]
        items_b = [i for s in line_b["stations"] for i in s["items"]]
        for item_a, item_b in zip(items_a, items_b, strict=True):
            if item_a != item_b:
                assert (item_a["state"], item_b["state"]) == ("prep", "done")
                changed.append(line_a["line_id"])
    assert sorted(set(changed)) == [
        "2026-10-05-line-e-plan",
        "2026-10-06-line-f-plan",
    ]
    pills = [line["status_pill"] for line in a["lines"]]
    assert pills.count("stopped") == 2  # the recorded floor's two stopped pills


# --------------------------------------------------------------------------- criterion 4: the real entry points


def _entry(run: Replay, argv: list[str], *, host: str | None = None) -> Any:
    """The entry the unit's ``tick`` and a turn's ``lines`` run, on this replay's seams."""
    return run_entry(
        argv,
        {"floor_fill": dict(fixture.OVERLAY)},
        host_id=host or fixture.HOST,
        build_handler=lambda _config: run.handler(),
    )


def test_lines_entry_real_entry_points_after_a_refusing_tick_prints_the_four_missing_lines() -> (
    None
):
    run = replay(variant="b", runner="refusing", ids="key-safe")
    ticked = _entry(run, ["tick"])
    printed = _entry(run, ["lines"])
    missing = [
        line
        for line in printed.stdout.splitlines()
        if line.startswith("MISSING line-stopped:")
    ]
    assert len(missing) == 4
    assert printed.stdout == ticked.stdout  # the stored answer is what the tick printed
    assert printed.exit_code == ticked.exit_code == 0


def test_lines_entry_real_entry_points_after_a_working_tick_prints_the_filled_lines() -> (
    None
):
    run = replay(variant="b", runner="working", ids="key-safe")
    ticked = _entry(run, ["tick"])
    printed = _entry(run, ["lines"])
    assert printed.stdout == ticked.stdout
    assert "MISSING" not in printed.stdout
    assert [r.kind for r in ticked.answer.key_results].count("started") == 4


def test_lines_entry_real_entry_points_after_a_working_tick_prints_note_line_filled() -> (
    None
):
    """The plan's wording: after a tick that started a lane on each startable line, each prints ``NOTE line-filled:``."""
    run = replay(variant="b", runner="working", ids="key-safe")
    _entry(run, ["tick"])
    printed = _entry(run, ["lines"])
    assert printed.stdout.count("NOTE line-filled:") == 4, printed.stdout


def test_lines_entry_real_entry_points_stale_answer_prints_the_stale_line() -> None:
    run = replay(variant="b", runner="working", ids="key-safe")
    _entry(run, ["tick"])
    run.clock.advance(minutes=run.config.max_age_minutes + 1)
    printed = _entry(run, ["lines"])
    assert printed.stdout == (
        f"UNKNOWN floor-fill-answer-stale:{run.config.max_age_minutes + 1}m\n"
    )
    assert printed.exit_code != 0
    assert "MISSING" not in printed.stdout


def test_lines_entry_real_entry_points_answer_absent_before_any_tick() -> None:
    run = replay(variant="b", runner="working", ids="key-safe", starts=0)
    printed = _entry(run, ["lines"])
    assert printed.stdout == "UNKNOWN floor-fill-answer-absent\n"


def test_lines_entry_real_entry_points_a_timed_out_command_is_unreachable_and_exits_75() -> (
    None
):
    release = threading.Event()

    class Stalling(ScriptedFacts):
        def read_floor(self, url: str) -> str:
            release.wait(timeout=10)
            return ""

    run = replay(variant="b", runner="working", ids="key-safe", starts=0)
    run.facts = Stalling(None)  # type: ignore[assignment]
    config = dict(
        fixture.OVERLAY, command_timeout_seconds=1, reserved_timeout_seconds=5
    )
    try:
        result = run_entry(
            ["tick"],
            {"floor_fill": config},
            host_id=fixture.HOST,
            build_handler=lambda cfg: _handler_with(run, cfg),
        )
    finally:
        release.set()
    assert result.stdout == "UNKNOWN floor-fill-unreachable\n"
    assert result.exit_code == 75
    assert result.timed_out
    assert run.runner.calls == []  # a stalled tick started nothing


def _handler_with(run: Replay, cfg: Any) -> Any:
    from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
        HandlerFloorFillCommand,
    )

    run.config = cfg
    return HandlerFloorFillCommand(
        config=cfg, orchestrator=run.orchestrator(), runner=run.forward
    )


def test_lines_entry_real_entry_points_a_forward_that_fails_is_unreachable_and_exits_75() -> (
    None
):
    run = replay(variant="b", runner="working", ids="key-safe", starts=0)
    result = _entry(
        run, ["lines"], host="orchestrator-host"
    )  # not the fill host: forwarded, and the forward fails
    assert result.stdout == "UNKNOWN floor-fill-unreachable\n"
    assert result.exit_code == 75
    assert result.forwarded
    assert run.runner.calls == []


def test_overlay_age_bound_is_the_one_the_replay_uses() -> None:
    config = fixture.OVERLAY
    assert timedelta(minutes=config["max_age_minutes"]) > timedelta(
        seconds=config["tick_interval_seconds"]
    )


# --------------------------------------------------------------------------- criterion 2: inspect on a captured floor

# Every task `inspect` returns, read against the captured floor: state prep, a note starting ready, no pull request,
# not named by a live lane, not held by an entry, on a line with capacity left (net of the entries).
INSPECT_JQ = (
    ". as $f | $picked | all(.[] | select(.next != null); . as $r "
    "| ($f.lines[] | select(.line_id == $r.line) | .stations[].items[] | select(.id == $r.next)) as $item "
    '| $item.state == "prep" and ($item.note | startswith("ready")) and (($item.prs // []) | length == 0) '
    "and ([$f.floor.lanes[] | select(.station != null and .station.line_id == $r.line "
    "and .station.item_id == $r.next)] | length == 0) "
    'and (($held | index("floor:\\($r.line):\\($r.next)")) == null) and $r.capacity_left >= 1)'
)


def picked(answer: ModelFloorFillAnswer) -> list[dict[str, Any]]:
    """What `inspect` returned: each line's next task and the capacity left on the line."""
    out: list[dict[str, Any]] = []
    for line in answer.lines:
        if line.startswith("INSPECT {"):
            row = json.loads(line.removeprefix("INSPECT "))
            out.append(
                {
                    "line": row["line"],
                    "next": row["next"],
                    "capacity_left": row["capacity_left"],
                }
            )
    return out


def jq_check(floor: str, chosen: list[dict[str, Any]], held: list[str]) -> int:
    done = subprocess.run(
        [
            "jq",
            "-e",
            "--argjson",
            "picked",
            json.dumps(chosen),
            "--argjson",
            "held",
            json.dumps(held),
            INSPECT_JQ,
        ],
        input=floor,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_inspect_returns_only_tasks_the_captured_floor_shows_ready(
    variant: str,
) -> None:
    run = replay(variant=variant, runner="refusing", starts=0)
    answer = run.orchestrator().run(EnumFloorFillCommand.INSPECT)
    chosen = picked(answer)
    assert len([c for c in chosen if c["next"] is not None]) == VARIANTS[variant]
    assert jq_check(fixture.floor_text(variant), chosen, held=[]) == 0
    # inspect is read-only: nothing minted, started or written
    assert run.runner.calls == []
    assert [c for c in run.tickets.calls if c.startswith("mint")] == []
    assert run.ticket_store.tickets == {}
    assert run.rows()[fixture.SCOPE] == {"version": 0, "payload": None}
    assert answer.lines[-1] == f"INSPECT seq=0 observed_at={fixture.NOW.isoformat()}"


def test_inspect_does_not_return_a_task_an_entry_holds() -> None:
    # key-safe ids: an entry cannot be built for a dotted id (the reducer's key rule), see the recorded-floor tests
    run = replay(
        variant="a",
        runner="refusing",
        ids="key-safe",
        starts=0,
        overlay_changes={"max_fill_lanes_per_line": 2},
    )
    held_key = "floor:2026-10-01-line-a-plan:T0-1"
    held = ModelFloorFillState(
        seq=1,
        entries={
            held_key: ModelFloorFillEntry(
                key=held_key,
                state=EnumFloorFillEntryState.RESERVED,
                lane="floor-held-lane",
                attempt=1,
                ticket="OMN-900001",
                reserved_at=fixture.NOW,
            )
        },
    )
    assert run.store.compare_and_set(fixture.SCOPE, 0, 1, held.model_dump_json())
    answer = run.orchestrator().run(EnumFloorFillCommand.INSPECT)
    chosen = picked(answer)
    line_a = next(c for c in chosen if c["line"] == "2026-10-01-line-a-plan")
    assert (
        line_a["next"] == "T0-2"
    )  # T0-1 is held by the entry, and one slot of two is left on the line
    assert line_a["capacity_left"] == 1
    assert jq_check(fixture.key_safe_inputs("a")[0], chosen, held=[held_key]) == 0
    assert answer.lines[-1].startswith("INSPECT seq=1 ")


def test_inspect_check_fails_for_a_task_the_floor_shows_active_with_a_pull_request() -> (
    None
):
    """The control: the `jq` check is red for a returned task the captured floor shows active and on a pull request."""
    chosen = [{"line": "2026-10-01-line-a-plan", "next": "TE.8", "capacity_left": 1}]
    assert jq_check(fixture.floor_text("a"), chosen, held=[]) != 0


def test_inspect_check_fails_for_a_held_task_and_for_a_full_line() -> None:
    first = {"line": "2026-10-01-line-a-plan", "next": "T0.1", "capacity_left": 1}
    held = ["floor:2026-10-01-line-a-plan:T0.1"]
    assert jq_check(fixture.floor_text("a"), [first], held=[]) == 0
    assert jq_check(fixture.floor_text("a"), [first], held=held) != 0
    assert (
        jq_check(fixture.floor_text("a"), [{**first, "capacity_left": 0}], held=[]) != 0
    )


# --------------------------------------------------------------------------- the failing controls of criterion 1


def test_count_pin_control_a_pool_with_three_free_slots_starts_three_not_six() -> None:
    """The control: the count the replay pins is red when the pool holds fewer slots than the startable lines."""
    run = replay(variant="a", runner="working", ids="key-safe", starts=3)
    run.pool.free = 3
    answer = tick(run)
    assert [r.kind for r in answer.key_results].count("started") == 3 != VARIANTS["a"]


def test_determinism_control_a_replay_on_another_clock_differs() -> None:
    """The control: the byte comparison is red for two replays that do not start from identical state."""
    first, second = (
        replay(variant="a", runner="working", ids="key-safe") for _ in range(2)
    )
    second.clock.advance(minutes=1)
    assert canonical(first, tick(first)) != canonical(second, tick(second))
