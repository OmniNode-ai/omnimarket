# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The walk's lifecycle controls: the call recorder graded against the Endpoint Security observer.

The fixtures are real measurements: a recorder stream per tree, the observer's exec, fork and
exit events, and the packet capture. The graded tree must read WORKING; the two control trees
(an exec through a shell, and a re-exec of the same program) must read blind, and each injected
break flips ``lifecycle_controls`` from passed to failed with a named reason. Every check is
keyed on the execution identity (pid, pidversion), never on pid alone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_walk_lifecycle_controls_compute.handlers.handler_lifecycle_controls import (
    LIFECYCLE_CONTROLS_OVERLAY_ID,
    HandlerLifecycleControls,
    load_lifecycle_controls_overlay,
)
from omnimarket.nodes.node_walk_lifecycle_controls_compute.models.model_lifecycle_controls import (
    ModelCaptureEvidence,
    ModelLifecycleControlsRequest,
    ModelLifecycleControlsResult,
    ModelLifecycleControlsSettings,
    ModelTreeInput,
    ModelTreeResult,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_walk_lifecycle_controls_compute"
)
SETTINGS = ModelLifecycleControlsSettings(
    blocked_destination="192.0.2.1:443",
    lookup_name="example.com",
    forked_child_call="connect",
)
# The observer's and the recorder's timebase: 24 MHz.
NUMER, DENOM = 125, 3

Ident = tuple[int, int]


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rows(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _root(name: str) -> Ident:
    """The measured root of a tree: the identity of the exec target that opens it."""
    root = json.loads(_text("roots.json"))[name]
    return (root["root_pid"], root["root_pidversion"])


GRADED_ROOT = _root("graded")
EXEC_SH_ROOT = _root("ctl-exec-sh")
REEXEC_ROOT = _root("ctl-reexec")


def _token(token: dict[str, int]) -> Ident:
    return (token["pid"], token["pidversion"])


def _observer_events() -> list[dict[str, Any]]:
    return sorted(
        _rows(_text("observer_eslogger_slim.jsonl")),
        key=lambda event: event["global_seq_num"],
    )


def _exec_targets() -> list[tuple[dict[str, Any], Ident, str]]:
    return [
        (
            event,
            _token(event["event"]["exec"]["target"]["audit_token"]),
            event["event"]["exec"]["target"]["executable"]["path"],
        )
        for event in _observer_events()
        if event["event_type"] == 9
    ]


def _exec_ticks(target: Ident) -> int:
    [ticks] = [
        event["mach_time"] for event, ident, _ in _exec_targets() if ident == target
    ]
    return int(ticks)


def _end_ticks(target: Ident) -> int:
    """The ticks of the first exec or exit the executed identity makes after its own exec."""
    start = _exec_ticks(target)
    later = [
        event["mach_time"]
        for event in _observer_events()
        if event["event_type"] in (9, 15)
        and _token(event["process"]["audit_token"]) == target
        and event["mach_time"] > start
    ]
    return int(min(later))


def _inside_ticks(target: Ident) -> int:
    """A time strictly between the identity's exec and its next exec or exit."""
    return (_exec_ticks(target) + _end_ticks(target)) // 2


def _line_identity(rows: list[dict[str, Any]], **cells: Any) -> Ident:
    [row] = [row for row in rows if all(row.get(k) == v for k, v in cells.items())]
    return (row["pid"], row["pidversion"])


def _graded_rows() -> list[dict[str, Any]]:
    return _rows(_text("recorder_graded.jsonl"))


# The graded member that calls fork(), the child it forks, and the last exec that loads.
GRADED_FORKER = _line_identity(_graded_rows(), t="call", call="fork")
GRADED_FORKED_CHILD = _line_identity(_graded_rows(), t="child")
GRADED_LAST_LOAD = [row for row in _graded_rows() if row["t"] == "load"][-1]
GRADED_LAST_EXEC: Ident = (GRADED_LAST_LOAD["pid"], GRADED_LAST_LOAD["pidversion"])
# The control_tree exec chain of the re-exec control: loaded, never loaded, loaded.
REEXEC_CHAIN = [ident for _, ident, _ in _exec_targets() if ident[0] == REEXEC_ROOT[0]]
# The exec of /usr/bin/nc below the shell control, which the recorder never loaded.
NC_EXEC = next(ident for _, ident, path in _exec_targets() if path == "/usr/bin/nc")


def _dump(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"


class _Legs:
    """The three recorder streams and the observer stream, each mutable by a test."""

    def __init__(self) -> None:
        self.graded = _text("recorder_graded.jsonl")
        self.exec_sh = _text("recorder_ctl_exec_sh.jsonl")
        self.reexec = _text("recorder_ctl_reexec.jsonl")
        self.observer = _text("observer_eslogger_slim.jsonl")
        self.packets = 0
        self.dns = 0

    def request(self) -> ModelLifecycleControlsRequest:
        return ModelLifecycleControlsRequest(
            trees=(
                ModelTreeInput(
                    name="graded",
                    role="graded",
                    expect="working",
                    root_pid=GRADED_ROOT[0],
                    root_pidversion=GRADED_ROOT[1],
                    recorder_jsonl=self.graded,
                ),
                ModelTreeInput(
                    name="ctl_exec_sh",
                    role="control",
                    expect="blind-image",
                    root_pid=EXEC_SH_ROOT[0],
                    root_pidversion=EXEC_SH_ROOT[1],
                    recorder_jsonl=self.exec_sh,
                ),
                ModelTreeInput(
                    name="ctl_reexec",
                    role="control",
                    expect="blind-image",
                    root_pid=REEXEC_ROOT[0],
                    root_pidversion=REEXEC_ROOT[1],
                    recorder_jsonl=self.reexec,
                ),
            ),
            observer_jsonl=self.observer,
            observer_timebase_numer=NUMER,
            observer_timebase_denom=DENOM,
            capture=ModelCaptureEvidence(
                packets_to_blocked_destination=self.packets,
                dns_queries_from_graded_tree=self.dns,
            ),
            settings=SETTINGS,
        )

    def grade(self) -> ModelLifecycleControlsResult:
        return HandlerLifecycleControls().handle(self.request())


def _tree(result: ModelLifecycleControlsResult, name: str) -> ModelTreeResult:
    [found] = [tree for tree in result.trees if tree.name == name]
    return found


def _without(text: str, keep: Callable[[dict[str, Any]], bool]) -> str:
    return _dump([row for row in _rows(text) if keep(row)])


def _is(row: dict[str, Any], **cells: Any) -> bool:
    return all(row.get(key) == value for key, value in cells.items())


def _assert_flips(
    legs_factory: Callable[[], _Legs],
    break_it: Callable[[_Legs], None],
    reason: str,
) -> ModelLifecycleControlsResult:
    """The failing control: the unbroken input reads passed, the broken one fails by name."""
    good = legs_factory()
    assert good.grade().lifecycle_controls == "passed"
    broken = legs_factory()
    break_it(broken)
    result = broken.grade()
    assert result.lifecycle_controls == "failed"
    assert reason in result.failed_reasons
    return result


# --- the measured fixtures --------------------------------------------------------------


def _expected_counts_from_fixtures() -> dict[str, dict[str, int]]:
    """Count obligations independently of the handler, straight from the observer rows."""
    observer = _rows(_text("observer_eslogger_slim.jsonl"))
    counts: dict[str, dict[str, int]] = {}
    for name, root, recorder in (
        ("graded", GRADED_ROOT, "recorder_graded.jsonl"),
        ("ctl_exec_sh", EXEC_SH_ROOT, "recorder_ctl_exec_sh.jsonl"),
        ("ctl_reexec", REEXEC_ROOT, "recorder_ctl_reexec.jsonl"),
    ):
        # The tree's pids: every pid the recorder saw plus the observer's forks below the root.
        pids = {row["pid"] for row in _rows(_text(recorder))}
        started = False
        execs = forks = 0
        for event in sorted(observer, key=lambda e: e["global_seq_num"]):
            token = event["process"]["audit_token"]
            if event["event_type"] == 9:
                target = event["event"]["exec"]["target"]["audit_token"]
                if (target["pid"], target["pidversion"]) == root:
                    started = True
                if started and target["pid"] in pids:
                    execs += 1
            elif event["event_type"] == 11 and started and token["pid"] in pids:
                forks += 1
        counts[name] = {"execs": execs, "forks": forks}
    return counts


def test_lifecycle_controls_golden_the_measured_fixtures_read_passed() -> None:
    result = _Legs().grade()

    assert result.lifecycle_controls == "passed"
    assert result.failed_reasons == ()
    assert result.observer_events == len(_rows(_text("observer_eslogger_slim.jsonl")))

    graded = _tree(result, "graded")
    assert graded.verdict is EnumReadingVerdict.WORKING
    assert graded.reasons == ()
    assert (graded.exec_obligations_total, graded.exec_obligations_met) == (5, 5)
    assert graded.fork_events == 5
    assert {c.name: c.passed for c in graded.controls} == {
        "forked_child": True,
        "rejected_connect_recorded": True,
        "cache_served_lookup": True,
    }

    for name in ("ctl_exec_sh", "ctl_reexec"):
        control = _tree(result, name)
        assert control.verdict is EnumReadingVerdict.NOT_WORKING
        assert control.reasons == ("recorder-blind-image",)
        assert [(c.name, c.passed) for c in control.controls] == [
            ("blind_image_control", True)
        ]


def test_lifecycle_controls_golden_counts_match_the_measured_fixtures() -> None:
    result = _Legs().grade()
    expected = _expected_counts_from_fixtures()

    for name, total, met, forks in (
        ("graded", 5, 5, 5),
        # exec of control_tree, /bin/sh, /bin/bash, then the forked child's /usr/bin/nc.
        ("ctl_exec_sh", 4, 3, 1),
        # exec of control_tree, then two re-execs of the same program under the same pid.
        ("ctl_reexec", 3, 2, 0),
    ):
        tree = _tree(result, name)
        assert tree.exec_obligations_total == total == expected[name]["execs"]
        assert tree.exec_obligations_met == met
        assert tree.fork_events == forks == expected[name]["forks"]


def test_lifecycle_controls_golden_capture_fixture_has_no_graded_dns_and_no_blocked_packets() -> (
    None
):
    """The capture counts the golden request carries are measured from the pktap text."""
    graded_pids = {row["pid"] for row in _graded_rows()}
    blocked = graded_queries = outside_lookups = 0
    for line in _text("capture_pktap.txt").splitlines():
        match = re.search(r"\(proc [^:)]*:(\d+), eproc [^:)]*:(\d+)\)", line)
        assert match, line
        eproc = int(match.group(2))
        if " > 192.0.2.1." in line:
            blocked += 1
        if re.search(r"\b(A|AAAA)\? ", line) and eproc in graded_pids:
            graded_queries += 1
        if re.search(r"\b(A|AAAA)\? example\.com\.", line) and eproc not in graded_pids:
            outside_lookups += 1
    assert (blocked, graded_queries) == (0, 0)
    # The capture does hold two example.com queries; both belong to a dscacheutil that the
    # observer shows exec and exit before any tree root, so outside every graded tree.
    assert outside_lookups == 2
    first_root_ticks = min(
        _exec_ticks(ident) for ident in (GRADED_ROOT, EXEC_SH_ROOT, REEXEC_ROOT)
    )
    [dscacheutil] = [
        ident for _, ident, path in _exec_targets() if path == "/usr/bin/dscacheutil"
    ]
    assert dscacheutil[0] not in graded_pids
    assert _end_ticks(dscacheutil) < first_root_ticks


def test_lifecycle_controls_golden_observer_leading_control_chars_and_blank_lines_are_read() -> (
    None
):
    legs = _Legs()
    lines = legs.observer.splitlines()
    legs.observer = "\n\n".join("\x1e" + line for line in lines) + "\n\n"

    assert legs.grade().lifecycle_controls == "passed"


# --- injected breaks: each flips passed to failed with a named reason -----------------------


def test_lifecycle_controls_flips_a_gap_in_the_observer_sequence_is_lifecycle_unobserved() -> (
    None
):
    def drop_seq_20(legs: _Legs) -> None:
        legs.observer = _without(legs.observer, lambda row: row["global_seq_num"] != 20)

    result = _assert_flips(_Legs, drop_seq_20, "lifecycle-unobserved")

    assert result.failed_reasons == ("lifecycle-unobserved",)
    for tree in result.trees:
        assert tree.verdict is EnumReadingVerdict.NOT_WORKING
        assert tree.reasons == ("lifecycle-unobserved",)
    # A control tree that reads NOT_WORKING for another reason only is not a passing control.
    assert not all(c.passed for tree in result.trees for c in tree.controls)


def test_lifecycle_controls_flips_no_observer_event_at_all_is_lifecycle_unobserved() -> (
    None
):
    for text in ("", "\n\n", "not json at all\n"):
        legs = _Legs()
        legs.observer = text
        result = legs.grade()
        assert result.lifecycle_controls == "failed"
        assert result.failed_reasons == ("lifecycle-unobserved",)


def test_lifecycle_controls_flips_a_stream_that_misses_the_roots_is_unobserved() -> (
    None
):
    """Consecutive numbers alone are not enough: the stream must hold the trees it grades."""

    def keep_the_tail(legs: _Legs) -> None:
        # The last two events are consecutive, but no root of the three trees is among them.
        last = max(row["global_seq_num"] for row in _rows(legs.observer))
        legs.observer = _without(
            legs.observer, lambda row: row["global_seq_num"] >= last - 1
        )

    result = _assert_flips(_Legs, keep_the_tail, "lifecycle-unobserved")

    assert all(tree.reasons == ("lifecycle-unobserved",) for tree in result.trees)


def test_lifecycle_controls_flips_a_graded_load_line_deleted_is_recorder_blind_image() -> (
    None
):
    def drop_forker_load(legs: _Legs) -> None:
        # The member that calls fork(): its own load line, by pid and pidversion.
        legs.graded = _without(
            legs.graded,
            lambda row: (
                not _is(
                    row,
                    t="load",
                    pid=GRADED_FORKER[0],
                    pidversion=GRADED_FORKER[1],
                )
            ),
        )

    result = _assert_flips(_Legs, drop_forker_load, "recorder-blind-image")

    graded = _tree(result, "graded")
    assert graded.verdict is EnumReadingVerdict.NOT_WORKING
    assert "recorder-blind-image" in graded.reasons
    assert (graded.exec_obligations_total, graded.exec_obligations_met) == (5, 4)


def test_lifecycle_controls_flips_a_deleted_child_line_is_recorder_missed_fork() -> (
    None
):
    def drop_child(legs: _Legs) -> None:
        legs.graded = _without(legs.graded, lambda row: row["t"] != "child")

    result = _assert_flips(_Legs, drop_child, "recorder-missed-fork")

    # The forked child's own connect no longer sits under a reconciled fork either.
    assert result.failed_reasons == ("coverage-control-failed", "recorder-missed-fork")


def test_lifecycle_controls_flips_a_deleted_fork_call_line_is_recorder_missed_fork() -> (
    None
):
    def drop_fork_call(legs: _Legs) -> None:
        legs.graded = _without(
            legs.graded, lambda row: not (row["t"] == "call" and row["call"] == "fork")
        )

    result = _assert_flips(_Legs, drop_fork_call, "recorder-missed-fork")

    assert _tree(result, "graded").fork_events == 5


def test_lifecycle_controls_flips_a_deleted_posix_spawn_call_line_is_recorder_missed_fork() -> (
    None
):
    def drop_one_spawn(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        first = next(
            index for index, row in enumerate(rows) if row.get("call") == "posix_spawn"
        )
        del rows[first]
        legs.graded = _dump(rows)

    result = _assert_flips(_Legs, drop_one_spawn, "recorder-missed-fork")

    assert "recorder-blind-image" not in result.failed_reasons


def test_lifecycle_controls_flips_a_posix_spawn_call_after_the_fork_does_not_cover_it() -> (
    None
):
    """The spawn call must not be later than the fork event it is matched to."""

    def spawn_after_fork(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        for row in rows:
            if row.get("call") == "posix_spawn":
                row["mach_abs"] += 10_000_000
        legs.graded = _dump(rows)

    _assert_flips(_Legs, spawn_after_fork, "recorder-missed-fork")


def test_lifecycle_controls_flips_a_zero_timebase_denominator_is_recorder_clock_undeclared() -> (
    None
):
    def zero_denominator(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        rows[3]["tb_denom"] = 0
        legs.graded = _dump(rows)

    result = _assert_flips(_Legs, zero_denominator, "recorder-clock-undeclared")

    assert "recorder-clock-undeclared" in _tree(result, "graded").reasons


def test_lifecycle_controls_flips_a_missing_timebase_is_recorder_clock_undeclared() -> (
    None
):
    def drop_numerator(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        del rows[0]["tb_numer"]
        legs.graded = _dump(rows)

    _assert_flips(_Legs, drop_numerator, "recorder-clock-undeclared")


def test_lifecycle_controls_flips_a_control_with_an_undeclared_clock_is_not_a_passing_control() -> (
    None
):
    def zero_denominator(legs: _Legs) -> None:
        rows = _rows(legs.exec_sh)
        rows[0]["tb_denom"] = 0
        legs.exec_sh = _dump(rows)

    result = _assert_flips(_Legs, zero_denominator, "recorder-clock-undeclared")

    assert not all(c.passed for c in _tree(result, "ctl_exec_sh").controls)


def test_lifecycle_controls_flips_the_exec_sh_control_made_working_is_control_missed() -> (
    None
):
    def cover_the_nc_exec(legs: _Legs) -> None:
        # The nc exec ran between its exec event and its exit; load it inside that window.
        legs.exec_sh += _dump(
            [
                {
                    "t": "load",
                    "call": "load",
                    "pid": NC_EXEC[0],
                    "pidversion": NC_EXEC[1],
                    "ppid": EXEC_SH_ROOT[0],
                    "image": "/usr/bin/nc",
                    "mach_abs": _inside_ticks(NC_EXEC),
                    "tb_numer": NUMER,
                    "tb_denom": DENOM,
                    "detail": "",
                }
            ]
        )

    result = _assert_flips(_Legs, cover_the_nc_exec, "coverage-control-failed")

    control = _tree(result, "ctl_exec_sh")
    assert control.verdict is EnumReadingVerdict.WORKING
    assert [(c.name, c.passed) for c in control.controls] == [
        ("blind_image_control", False)
    ]
    # The control is what failed; the leg's own trees are unaffected.
    assert _tree(result, "graded").verdict is EnumReadingVerdict.WORKING
    assert "recorder-blind-image" not in result.failed_reasons


def test_lifecycle_controls_flips_the_reexec_control_made_working_is_control_missed() -> (
    None
):
    def cover_the_reexec(legs: _Legs) -> None:
        # The middle re-exec ran until the next exec of the same pid; load it inside that window.
        middle = REEXEC_CHAIN[1]
        legs.reexec += _dump(
            [
                {
                    "t": "load",
                    "call": "load",
                    "pid": middle[0],
                    "pidversion": middle[1],
                    "ppid": 1,
                    "image": "/tmp/control_tree",
                    "mach_abs": _inside_ticks(middle),
                    "tb_numer": NUMER,
                    "tb_denom": DENOM,
                    "detail": "",
                }
            ]
        )

    result = _assert_flips(_Legs, cover_the_reexec, "coverage-control-failed")

    assert _tree(result, "ctl_reexec").verdict is EnumReadingVerdict.WORKING


def test_lifecycle_controls_flips_a_load_for_the_pid_with_a_later_pidversion_does_not_cover() -> (
    None
):
    """Identity keyed, not pid keyed: the same pid at a later pidversion is another identity."""
    first, middle, later = REEXEC_CHAIN
    assert first == REEXEC_ROOT
    assert first[0] == middle[0] == later[0]
    legs = _Legs()
    # The unbroken fixture holds a load for the later identity and none for the middle one.
    reexec = _rows(legs.reexec)
    assert any(_is(row, t="load", pid=later[0], pidversion=later[1]) for row in reexec)
    assert not any(
        _is(row, t="load", pid=middle[0], pidversion=middle[1]) for row in reexec
    )
    baseline = legs.grade()
    assert baseline.lifecycle_controls == "passed"
    assert _tree(baseline, "ctl_reexec").reasons == ("recorder-blind-image",)

    # Keep only the later pidversion's load: the two earlier obligations are both uncovered.
    legs.reexec = _without(legs.reexec, lambda row: row["pidversion"] == later[1])
    only_later = legs.grade()
    tree = _tree(only_later, "ctl_reexec")
    assert (tree.exec_obligations_total, tree.exec_obligations_met) == (3, 1)
    assert tree.reasons == ("recorder-blind-image",)

    # A load for the later pidversion timed inside the middle identity's window still does not
    # cover the middle identity: the pid matches, the pidversion does not.
    legs = _Legs()
    legs.reexec += _dump(
        [
            {
                "t": "load",
                "call": "load",
                "pid": later[0],
                "pidversion": later[1],
                "ppid": 1,
                "image": "/tmp/control_tree",
                "mach_abs": _inside_ticks(middle),
                "tb_numer": NUMER,
                "tb_denom": DENOM,
                "detail": "",
            }
        ]
    )
    shadowed = legs.grade()
    tree = _tree(shadowed, "ctl_reexec")
    assert tree.reasons == ("recorder-blind-image",)
    assert (tree.exec_obligations_total, tree.exec_obligations_met) == (3, 2)
    assert shadowed.lifecycle_controls == "passed"


def test_lifecycle_controls_flips_a_packet_to_the_blocked_destination_is_control_failed() -> (
    None
):
    def one_packet(legs: _Legs) -> None:
        legs.packets = 1

    result = _assert_flips(_Legs, one_packet, "coverage-control-failed")

    graded = _tree(result, "graded")
    assert graded.verdict is EnumReadingVerdict.WORKING
    assert {c.name: c.passed for c in graded.controls}[
        "rejected_connect_recorded"
    ] is False


def test_lifecycle_controls_flips_a_graded_dns_query_is_control_failed() -> None:
    def one_query(legs: _Legs) -> None:
        legs.dns = 1

    result = _assert_flips(_Legs, one_query, "coverage-control-failed")

    assert {c.name: c.passed for c in _tree(result, "graded").controls}[
        "cache_served_lookup"
    ] is False


def test_lifecycle_controls_flips_one_lookup_line_dropped_is_control_failed() -> None:
    def drop_one_lookup(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        index = next(
            i for i, row in enumerate(rows) if row.get("call") == "getaddrinfo"
        )
        del rows[index]
        legs.graded = _dump(rows)

    result = _assert_flips(_Legs, drop_one_lookup, "coverage-control-failed")

    assert {c.name: c.passed for c in _tree(result, "graded").controls}[
        "cache_served_lookup"
    ] is False


def test_lifecycle_controls_flips_a_lookup_of_another_name_does_not_count() -> None:
    def other_name(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        for row in rows:
            if row.get("call") == "getaddrinfo":
                row["detail"] = "example.org"
        legs.graded = _dump(rows)

    _assert_flips(_Legs, other_name, "coverage-control-failed")


def test_lifecycle_controls_flips_no_recorded_connect_to_the_blocked_destination_is_control_failed() -> (
    None
):
    def drop_connects(legs: _Legs) -> None:
        legs.graded = _without(legs.graded, lambda row: row.get("call") != "connect")

    result = _assert_flips(_Legs, drop_connects, "coverage-control-failed")

    outcomes = {c.name: c.passed for c in _tree(result, "graded").controls}
    assert outcomes["rejected_connect_recorded"] is False
    assert outcomes["forked_child"] is False


def test_lifecycle_controls_flips_a_connect_not_under_the_forked_child_fails_that_control() -> (
    None
):
    def drop_child_connect(legs: _Legs) -> None:
        legs.graded = _without(
            legs.graded,
            lambda row: (
                not _is(
                    row,
                    call="connect",
                    pid=GRADED_FORKED_CHILD[0],
                    pidversion=GRADED_FORKED_CHILD[1],
                )
            ),
        )

    result = _assert_flips(_Legs, drop_child_connect, "coverage-control-failed")

    outcomes = {c.name: c.passed for c in _tree(result, "graded").controls}
    # The connect under the spawned child is still a recorded rejected connect, but not a
    # forked child's.
    assert outcomes == {
        "forked_child": False,
        "rejected_connect_recorded": True,
        "cache_served_lookup": True,
    }


def test_lifecycle_controls_flips_a_recorder_image_that_differs_is_blind_image() -> (
    None
):
    def wrong_image(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        for row in rows:
            if _is(
                row, t="load", pid=GRADED_LAST_EXEC[0], pidversion=GRADED_LAST_EXEC[1]
            ):
                row["image"] = "/usr/bin/other"
        legs.graded = _dump(rows)

    result = _assert_flips(_Legs, wrong_image, "recorder-blind-image")

    assert _tree(result, "graded").exec_obligations_met == 4


def test_lifecycle_controls_flips_a_private_prefix_on_the_recorder_image_is_normalised() -> (
    None
):
    legs = _Legs()
    rows = _rows(legs.graded)
    for row in rows:
        if row.get("image") == "/tmp/control_tree":
            row["image"] = "/private/tmp/control_tree"
    legs.graded = _dump(rows)

    assert legs.grade().lifecycle_controls == "passed"


def test_lifecycle_controls_flips_a_load_before_its_exec_event_does_not_cover_it() -> (
    None
):
    def load_too_early(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        for row in rows:
            if _is(
                row, t="load", pid=GRADED_LAST_EXEC[0], pidversion=GRADED_LAST_EXEC[1]
            ):
                # The exec event's own time: not later than the exec.
                row["mach_abs"] = _exec_ticks(GRADED_LAST_EXEC)
        legs.graded = _dump(rows)

    _assert_flips(_Legs, load_too_early, "recorder-blind-image")


def test_lifecycle_controls_flips_a_load_after_the_identity_exited_does_not_cover_it() -> (
    None
):
    def load_too_late(legs: _Legs) -> None:
        rows = _rows(legs.graded)
        for row in rows:
            if _is(
                row, t="load", pid=GRADED_LAST_EXEC[0], pidversion=GRADED_LAST_EXEC[1]
            ):
                # The identity's own exit: a load must come before it.
                row["mach_abs"] = _end_ticks(GRADED_LAST_EXEC)
        legs.graded = _dump(rows)

    _assert_flips(_Legs, load_too_late, "recorder-blind-image")


def test_lifecycle_controls_flips_a_recorder_execve_call_line_opens_no_obligation() -> (
    None
):
    """Only observer exec events open obligations: execve lines in the stream add none."""
    result = _Legs().grade()
    execve_lines = [
        row
        for row in _rows(_text("recorder_ctl_exec_sh.jsonl"))
        if row["call"] == "execve"
    ]
    assert len(execve_lines) == 3
    assert _tree(result, "ctl_exec_sh").exec_obligations_total == 4


# --- synthetic trees --------------------------------------------------------------------------


def _exec(
    seq: int, t: int, caller: tuple[int, int], target: tuple[int, int], path: str
) -> dict[str, Any]:
    return {
        "event_type": 9,
        "global_seq_num": seq,
        "mach_time": t,
        "process": {"audit_token": {"pid": caller[0], "pidversion": caller[1]}},
        "event": {
            "exec": {
                "target": {
                    "audit_token": {"pid": target[0], "pidversion": target[1]},
                    "executable": {"path": path},
                }
            }
        },
    }


def _fork(
    seq: int, t: int, parent: tuple[int, int], child: tuple[int, int]
) -> dict[str, Any]:
    return {
        "event_type": 11,
        "global_seq_num": seq,
        "mach_time": t,
        "process": {"audit_token": {"pid": parent[0], "pidversion": parent[1]}},
        "event": {
            "fork": {
                "child": {"audit_token": {"pid": child[0], "pidversion": child[1]}}
            }
        },
    }


def _exit(seq: int, t: int, who: tuple[int, int]) -> dict[str, Any]:
    return {
        "event_type": 15,
        "global_seq_num": seq,
        "mach_time": t,
        "process": {"audit_token": {"pid": who[0], "pidversion": who[1]}},
        "event": {"exit": {}},
    }


def _line(
    kind: str,
    call: str,
    who: tuple[int, int],
    mach: int,
    *,
    image: str = "/bin/x",
    detail: str = "",
    numer: int = 1,
    denom: int = 1,
) -> dict[str, Any]:
    return {
        "t": kind,
        "call": call,
        "pid": who[0],
        "pidversion": who[1],
        "ppid": 1,
        "image": image,
        "mach_abs": mach,
        "tb_numer": numer,
        "tb_denom": denom,
        "detail": detail,
    }


def _synthetic(
    observer: list[dict[str, Any]],
    recorder: list[dict[str, Any]],
    *,
    root: tuple[int, int] = (100, 1),
    numer: int = 1,
    denom: int = 1,
    connect_as: tuple[int, int] | None = None,
) -> ModelLifecycleControlsResult:
    """One graded tree with the lookups and connect the graded controls need already holding."""
    covering = [
        _line(
            "call", "connect", connect_as or root, 9_000, detail="inet 192.0.2.1:443"
        ),
        _line("call", "getaddrinfo", root, 9_001, detail="example.com"),
        _line("call", "getaddrinfo", root, 9_002, detail="example.com"),
    ]
    return HandlerLifecycleControls().handle(
        ModelLifecycleControlsRequest(
            trees=(
                ModelTreeInput(
                    name="syn",
                    role="graded",
                    expect="working",
                    root_pid=root[0],
                    root_pidversion=root[1],
                    recorder_jsonl=_dump(recorder + covering),
                ),
            ),
            observer_jsonl=_dump(observer),
            observer_timebase_numer=numer,
            observer_timebase_denom=denom,
            capture=ModelCaptureEvidence(
                packets_to_blocked_destination=0, dns_queries_from_graded_tree=0
            ),
            settings=SETTINGS,
        )
    )


def test_lifecycle_controls_synthetic_a_covered_tree_reads_working() -> None:
    root = (100, 1)
    child = (101, 2)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _fork(1, 20, root, child),
        _exit(2, 30, child),
        _exit(3, 40, root),
    ]
    recorder = [
        _line("load", "load", root, 10),
        _line("call", "fork", root, 15),
        _line("child", "fork", child, 21),
    ]
    # The forked-child control needs a connect under the reconciled child.
    result = _synthetic(observer, recorder, connect_as=child)
    assert result.lifecycle_controls == "passed"
    assert _tree(result, "syn").verdict is EnumReadingVerdict.WORKING
    # With the connect under the root only, the tree still reads working, but the control fails.
    root_only = _synthetic(observer, recorder)
    assert _tree(root_only, "syn").verdict is EnumReadingVerdict.WORKING
    assert root_only.failed_reasons == ("coverage-control-failed",)


def test_lifecycle_controls_synthetic_blind_child_is_a_fork_child_no_recorder_line_carries() -> (
    None
):
    root = (100, 1)
    child = (101, 2)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _fork(1, 20, root, child),
        _exit(2, 30, child),
        _exit(3, 40, root),
    ]
    recorder = [_line("load", "load", root, 10), _line("call", "posix_spawn", root, 15)]
    result = _synthetic(observer, recorder)

    tree = _tree(result, "syn")
    assert tree.verdict is EnumReadingVerdict.NOT_WORKING
    assert tree.reasons == ("recorder-blind-child", "recorder-missed-fork")
    assert "recorder-blind-child" in result.failed_reasons


def test_lifecycle_controls_synthetic_a_fork_child_that_execs_is_not_blind_child() -> (
    None
):
    """The child that execs is covered by its exec obligation, not by a line for its pre-exec identity."""
    root = (100, 1)
    child = (101, 2)
    target = (101, 3)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _fork(1, 20, root, child),
        _exec(2, 22, child, target, "/bin/y"),
        _exit(3, 30, target),
        _exit(4, 40, root),
    ]
    recorder = [
        _line("load", "load", root, 10),
        _line("call", "posix_spawn", root, 15),
        _line("load", "load", target, 25, image="/bin/y"),
    ]
    tree = _tree(_synthetic(observer, recorder), "syn")

    assert tree.verdict is EnumReadingVerdict.WORKING
    assert (tree.exec_obligations_total, tree.exec_obligations_met) == (2, 2)


def test_lifecycle_controls_synthetic_a_superseded_identity_is_blind_image_not_blind_child() -> (
    None
):
    root = (100, 1)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _exec(1, 20, root, (100, 2), "/bin/x"),
        _exit(2, 30, (100, 2)),
    ]
    recorder = [_line("load", "load", root, 10)]
    tree = _tree(_synthetic(observer, recorder), "syn")

    assert tree.reasons == ("recorder-blind-image",)
    assert (tree.exec_obligations_total, tree.exec_obligations_met) == (2, 1)


def test_lifecycle_controls_synthetic_one_load_line_closes_at_most_one_obligation() -> (
    None
):
    root = (100, 1)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _exec(1, 20, root, (100, 2), "/bin/x"),
        _exit(2, 30, (100, 2)),
    ]
    # One line for (100, 2) cannot also stand for the root's obligation.
    recorder = [_line("load", "load", (100, 2), 25)]
    tree = _tree(_synthetic(observer, recorder), "syn")

    assert tree.reasons == ("recorder-blind-image",)
    assert (tree.exec_obligations_total, tree.exec_obligations_met) == (2, 1)


def test_lifecycle_controls_synthetic_each_clock_converts_with_its_own_timebase() -> (
    None
):
    root = (100, 1)
    observer = [
        _exec(0, 40_000, (100, 0), root, "/bin/x"),
        _exit(1, 50_000, root),
    ]
    # 1000 ticks at 125/3 is 41666 ns: after the exec (40000 ns), before the exit (50000 ns).
    on_time = [_line("load", "load", root, 1000, numer=125, denom=3)]
    tree = _tree(_synthetic(observer, on_time), "syn")
    assert tree.verdict is EnumReadingVerdict.WORKING

    # 900 ticks at 125/3 is 37500 ns: before the exec. Raw ticks (900 < 40000) say the same,
    # so also place a line whose raw tick count would pass but whose converted time does not.
    early = [_line("load", "load", root, 900, numer=125, denom=3)]
    assert _tree(_synthetic(observer, early), "syn").reasons == (
        "recorder-blind-image",
    )
    late = [_line("load", "load", root, 41_000, numer=125, denom=3)]
    assert _tree(_synthetic(observer, late), "syn").reasons == ("recorder-blind-image",)


def test_lifecycle_controls_synthetic_the_observer_timebase_is_applied_to_observer_ticks() -> (
    None
):
    root = (100, 1)
    observer = [
        _exec(0, 1200, (100, 0), root, "/bin/x"),
        _exit(1, 1600, root),
    ]
    # Observer ticks at 125/3: exec 50000 ns, exit 66666 ns. A recorder at 1/1 loading at
    # 60000 ns sits between them.
    recorder = [_line("load", "load", root, 60_000)]
    tree = _tree(_synthetic(observer, recorder, numer=125, denom=3), "syn")
    assert tree.verdict is EnumReadingVerdict.WORKING
    # The same ticks read at 1/1 would put the exec at 1200 ns and the load after the exit.
    assert (
        _tree(_synthetic(observer, recorder, numer=1, denom=1), "syn").verdict
        is EnumReadingVerdict.NOT_WORKING
    )


def test_lifecycle_controls_synthetic_a_tree_whose_root_exec_was_never_observed_is_unobserved() -> (
    None
):
    observer = [_exec(0, 5, (200, 0), (200, 1), "/bin/z")]
    recorder = [_line("load", "load", (100, 1), 10)]
    result = _synthetic(observer, recorder)

    assert result.lifecycle_controls == "failed"
    assert _tree(result, "syn").reasons == ("lifecycle-unobserved",)
    assert "lifecycle-unobserved" in result.failed_reasons


def test_lifecycle_controls_synthetic_events_of_a_closed_identity_add_nothing() -> None:
    root = (100, 1)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        _exit(1, 20, root),
        # The same pid, reused after exit with a new pidversion, is not a member.
        _exec(2, 30, (100, 7), (100, 8), "/bin/other"),
    ]
    recorder = [_line("load", "load", root, 10)]
    tree = _tree(_synthetic(observer, recorder), "syn")

    assert tree.verdict is EnumReadingVerdict.WORKING
    assert tree.exec_obligations_total == 1


def test_lifecycle_controls_synthetic_a_fork_by_a_non_member_with_a_member_pid_is_ignored() -> (
    None
):
    root = (100, 1)
    observer = [
        _exec(0, 5, (100, 0), root, "/bin/x"),
        # Same pid as the root, but another pidversion: not an identity of this tree.
        _fork(1, 20, (100, 99), (150, 5)),
        _exit(2, 40, root),
    ]
    recorder = [_line("load", "load", root, 10)]
    tree = _tree(_synthetic(observer, recorder), "syn")

    assert tree.verdict is EnumReadingVerdict.WORKING
    assert tree.fork_events == 0


def test_lifecycle_controls_request_refuses_a_malformed_shape() -> None:
    legs = _Legs()
    good = legs.request()
    with pytest.raises(ValidationError):
        ModelLifecycleControlsRequest(
            **{**good.model_dump(), "observer_timebase_denom": 0}
        )
    with pytest.raises(ValidationError):
        ModelTreeInput(
            name="x",
            role="graded",
            expect="blind-image",
            root_pid=1,
            root_pidversion=1,
            recorder_jsonl="",
        )
    with pytest.raises(ValidationError):
        ModelLifecycleControlsRequest(**{**good.model_dump(), "trees": good.trees[1:]})
    with pytest.raises(ValidationError):
        ModelLifecycleControlsRequest(
            **{**good.model_dump(), "trees": (good.trees[0], good.trees[0])}
        )


def test_lifecycle_controls_result_carries_only_token_safe_reasons_never_raw_lines() -> (
    None
):
    legs = _Legs()
    rows = _rows(legs.graded)
    rows[0]["detail"] = "SECRET-DETAIL-VALUE"
    rows[0]["tb_denom"] = 0
    legs.graded = _dump(rows)
    result = legs.grade()

    assert "SECRET-DETAIL-VALUE" not in result.model_dump_json()
    assert all(re.fullmatch(r"[a-z-]+", reason) for reason in result.failed_reasons)


# --- the contract and the overlay -----------------------------------------------------------------


def test_lifecycle_controls_contract_routes_the_grading_operation() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    routes = {
        entry["operation"]: entry for entry in contract["handler_routing"]["handlers"]
    }

    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["name"] == "node_walk_lifecycle_controls_compute"
    assert routes["grade_lifecycle_controls"]["handler"]["name"] == (
        "HandlerLifecycleControls"
    )
    assert routes["grade_lifecycle_controls"]["input_model"].endswith(
        ".ModelLifecycleControlsRequest"
    )
    assert routes["grade_lifecycle_controls"]["output_model"].endswith(
        ".ModelLifecycleControlsResult"
    )
    assert contract["overlay"]["id"] == LIFECYCLE_CONTROLS_OVERLAY_ID


def test_lifecycle_controls_settings_come_from_the_overlay_with_no_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(NodeOverlayNotFoundError):
        load_lifecycle_controls_overlay()
    assert not (NODE_DIR / "overlay.yaml").exists()

    install_node_overlay(
        monkeypatch,
        tmp_path,
        LIFECYCLE_CONTROLS_OVERLAY_ID,
        SETTINGS.model_dump(),
    )
    assert load_lifecycle_controls_overlay() == SETTINGS

    install_node_overlay(
        monkeypatch,
        tmp_path,
        LIFECYCLE_CONTROLS_OVERLAY_ID,
        {**SETTINGS.model_dump(), "lookup_name": ""},
    )
    with pytest.raises(NodeOverlayError):
        load_lifecycle_controls_overlay()

    install_node_overlay(
        monkeypatch,
        tmp_path,
        LIFECYCLE_CONTROLS_OVERLAY_ID,
        {"blocked_destination": "192.0.2.1:443"},
    )
    with pytest.raises(NodeOverlayError):
        load_lifecycle_controls_overlay()


# --- through the runtime --------------------------------------------------------------------------


def _hosted(
    tmp_path: Path, request: ModelLifecycleControlsRequest
) -> tuple[object, ModelLifecycleControlsResult]:
    from tests.runtime_local_compat import RuntimeLocal

    input_path = tmp_path / "input.json"
    input_path.write_text(request.model_dump_json())
    hosted = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=10,
    )
    outcome = hosted.run()
    result = hosted.handler_result
    assert isinstance(result, ModelLifecycleControlsResult)
    return outcome, result


def test_lifecycle_controls_golden_chain_runtime_reads_passed(tmp_path: Path) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    outcome, result = _hosted(tmp_path, _Legs().request())

    assert outcome == EnumWorkflowResult.COMPLETED
    assert result.lifecycle_controls == "passed"
    assert result.failed_reasons == ()
    assert _tree(result, "graded").exec_obligations_met == 5


def test_lifecycle_controls_error_chain_runtime_a_gap_ridden_observer_publishes_failed(
    tmp_path: Path,
) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    legs = _Legs()
    legs.observer = _without(legs.observer, lambda row: row["global_seq_num"] != 20)
    outcome, result = _hosted(tmp_path, legs.request())

    assert outcome == EnumWorkflowResult.COMPLETED
    assert result.lifecycle_controls == "failed"
    assert result.failed_reasons == ("lifecycle-unobserved",)


# --- the real uv-managed onex as a second graded tree --------------------------------------

ONEX_ROOT = _root("onex")


def _with_onex(legs: _Legs, onex_recorder: str) -> ModelLifecycleControlsRequest:
    base = legs.request()
    onex = ModelTreeInput(
        name="onex",
        role="graded",
        expect="working",
        root_pid=ONEX_ROOT[0],
        root_pidversion=ONEX_ROOT[1],
        recorder_jsonl=onex_recorder,
    )
    return base.model_copy(update={"trees": (*base.trees, onex)})


def test_lifecycle_controls_golden_the_real_onex_loads_the_recorder_and_reads_working() -> (
    None
):
    result = HandlerLifecycleControls().handle(
        _with_onex(_Legs(), _text("recorder_graded_onex.jsonl"))
    )

    assert result.lifecycle_controls == "passed"
    onex = _tree(result, "onex")
    assert onex.verdict is EnumReadingVerdict.WORKING
    # One exec obligation: the uv-managed CPython the onex launcher runs, named by its real path.
    assert (onex.exec_obligations_total, onex.exec_obligations_met) == (1, 1)


def test_lifecycle_controls_the_real_onex_without_its_load_line_fails_blind_image() -> (
    None
):
    good = HandlerLifecycleControls().handle(
        _with_onex(_Legs(), _text("recorder_graded_onex.jsonl"))
    )
    assert good.lifecycle_controls == "passed"

    broken = HandlerLifecycleControls().handle(_with_onex(_Legs(), ""))

    assert broken.lifecycle_controls == "failed"
    assert "recorder-blind-image" in broken.failed_reasons
    assert _tree(broken, "onex").verdict is EnumReadingVerdict.NOT_WORKING


def test_lifecycle_controls_the_contract_declares_one_graded_terminal_topic() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))

    assert (
        contract["runtime_dispatch"]["terminal_events"]["success"]
        == "onex.evt.omnimarket.walk-lifecycle-controls-graded.v1"
    )
    assert (
        contract["terminal_event"]
        == contract["runtime_dispatch"]["terminal_events"]["success"]
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
