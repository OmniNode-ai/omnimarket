# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 live run (OMN-23087): real heads from the PR watcher's state, judged the way the floor judges the join.

The run writes piece 8's chain and piece 9's answer to one journal under one correlation id; the floor's own receipt fold reads
the journal. A head the decision merges reads the join ``golden_only``, a head the lab check holds ``error_only``, both
``passed``. The failing controls (``blind-decision`` and ``silent-landing``) read ``broken``, and the control tests show the
same fold reading ``passed`` without the break, so the red is the break's.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink, JsonlEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_pr_landing_decision_compute import runtime_join_j8
from omnimarket.nodes.node_pr_landing_decision_compute.adapters.watcher_lab_check import (
    WatcherLabCheckCollector,
    WatcherStateError,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    load_join_j8_policy,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8_live import (
    HandlerLandingJoinJ8Live,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8 import (
    EnumJoinJ8Outcome,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8_live import (
    EnumJ8Break,
    EnumJ8LiveStatus,
    EnumJ8Want,
    ModelJoinJ8LiveCommand,
    ModelJoinJ8LiveOverlay,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 6, 0, 0, tzinfo=UTC)
POLICY = load_join_j8_policy()
CHECK = "lab-tests / run"
SHA_A = "7265930bafc673e66ced5bb67b9464bc460f1d77"
SHA_B = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
SHA_C = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
SHA_D = "1111111111111111111111111111111111111111"
CHAIN_DEADLINE_S = 900


def _overlay(**over: Any) -> ModelJoinJ8LiveOverlay:
    base: dict[str, Any] = {
        "watcher_state_path": "/unused/state.json",
        "watcher_max_age_seconds": 900,
        "owner": "OmniNode-ai",
        "lab_check_name": CHECK,
        "lab_bound_repos": ["omnimarket"],
        "max_candidates": 20,
        "poster_node": "node_lab_check_poster",
        "poster_failure_code": "lab_check_not_passed",
        "emission_journal": "/unused/emissions.jsonl",
        "control_journal": "/unused/controls.jsonl",
        "drill": False,
        "allow_injected_break": True,
    }
    return ModelJoinJ8LiveOverlay.model_validate({**base, **over})


def _record(
    number: int,
    head: str,
    *,
    runs: list[list[str]] | None,
    read_on: str | None = None,
    verdict: str = "GREEN",
    merge_state: str = "CLEAN",
    repo: str = "omnimarket",
    updated: str = "2026-11-08T05:50:00Z",
    state: str = "OPEN",
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "facts": {
            "repo": repo,
            "number": number,
            "head_sha": head,
            "state": state,
            "draft": draft,
            "created_at": "2026-11-08T05:00:00Z",
            "updated_at": updated,
        },
        "ci": None
        if runs is None
        else {"sha": read_on or head, "verdict": verdict, "runs": runs},
        "review": {"merge_state": merge_state},
    }


def _ok(name: str = CHECK) -> list[str]:
    return [name, "completed", "success", "2026-11-08T05:55:00Z"]


def _state(**records: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_tick": (NOW - timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
        "prs": {f"{k.replace('_', '#')}": v for k, v in records.items()},
    }


def _good() -> dict[str, Any]:
    return _record(11, SHA_A, runs=[_ok()])


def _held_missing() -> dict[str, Any]:
    return _record(12, SHA_B, runs=[["other / check", "completed", "success", "t"]])


class _Clock:
    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _run(
    state: dict[str, Any],
    cid: str,
    want: EnumJ8Want,
    sink: InMemoryEmissionSink,
    *,
    inject: EnumJ8Break | None = None,
    overlay: ModelJoinJ8LiveOverlay | None = None,
    pr: str | None = None,
):
    ov = overlay or _overlay()
    handler = HandlerLandingJoinJ8Live(
        subjects=WatcherLabCheckCollector(state, ov, NOW, inject),
        sink=sink,
        overlay=ov,
        policy=POLICY,
        clock=_Clock(),
        backend="in_memory",
    )
    return handler.handle(
        ModelJoinJ8LiveCommand(correlation_id=cid, want=want, pr=pr, inject=inject)
    )


def _fold(sink: InMemoryEmissionSink, now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(sink.events),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="ci to landing",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


# -------------------------------------------------------------------------------------------------- golden


def test_golden_a_real_green_head_with_its_lab_check_is_merged_and_the_join_reads_golden() -> (
    None
):
    sink = InMemoryEmissionSink()
    state = _state(omnimarket_11=_good(), omnimarket_12=_held_missing())
    result = _run(state, "cid-golden", EnumJ8Want.GOLDEN, sink)
    assert result.status is EnumJ8LiveStatus.COMPLETED
    assert result.pr == "OmniNode-ai/omnimarket#11"
    assert result.outcome is EnumJoinJ8Outcome.MERGEABLE
    assert result.poster_ended == "success"
    assert _fold(sink, NOW + timedelta(minutes=1)) == ("golden_only", [])


def test_golden_wants_a_head_the_decision_merges_not_just_a_passing_check() -> None:
    sink = InMemoryEmissionSink()
    behind = _record(13, SHA_C, runs=[_ok()], merge_state="BEHIND")
    result = _run(_state(omnimarket_13=behind), "cid-g2", EnumJ8Want.GOLDEN, sink)
    assert result.status is EnumJ8LiveStatus.NO_SUBJECT
    assert sink.events == []


# -------------------------------------------------------------------------------------------------- error


@pytest.mark.parametrize(
    ("record", "verdict"),
    [
        (_held_missing(), EnumLabCheckVerdict.MISSING),
        (
            _record(14, SHA_C, runs=[["lab-tests / run", "completed", "failure", "t"]]),
            EnumLabCheckVerdict.FAILED,
        ),
        # The runs were read on an older head: the check is not on this head.
        (_record(15, SHA_D, runs=[_ok()], read_on=SHA_C), EnumLabCheckVerdict.MISSING),
    ],
)
def test_error_a_real_head_the_lab_check_holds_reads_the_join_error_only(
    record: dict[str, Any], verdict: EnumLabCheckVerdict
) -> None:
    sink = InMemoryEmissionSink()
    number = record["facts"]["number"]
    result = _run(
        _state(**{f"omnimarket_{number}": record}),
        "cid-error",
        EnumJ8Want.ERROR,
        sink,
    )
    assert result.status is EnumJ8LiveStatus.COMPLETED
    assert result.verdict is verdict
    assert result.poster_ended == "failure:lab_check_not_passed"
    assert _fold(sink, NOW + timedelta(minutes=1)) == ("error_only", [])


def test_golden_and_error_runs_together_read_passed() -> None:
    sink = InMemoryEmissionSink()
    state = _state(omnimarket_11=_good(), omnimarket_12=_held_missing())
    _run(state, "cid-1", EnumJ8Want.GOLDEN, sink)
    _run(state, "cid-2", EnumJ8Want.ERROR, sink)
    assert _fold(sink, NOW + timedelta(minutes=1)) == ("passed", [])


def test_a_check_still_running_inside_its_bound_emits_nothing() -> None:
    sink = InMemoryEmissionSink()
    running = _record(
        16, SHA_C, runs=[[CHECK, "in_progress", "", "t"]], verdict="PENDING"
    )
    result = _run(_state(omnimarket_16=running), "cid-p", EnumJ8Want.ANY, sink)
    assert result.status is EnumJ8LiveStatus.PENDING
    assert sink.events == []


def test_only_open_heads_of_lab_bound_repositories_are_subjects() -> None:
    sink = InMemoryEmissionSink()
    state = _state(
        other_1=_record(1, SHA_A, runs=[_ok()], repo="other"),
        omnimarket_2=_record(2, SHA_B, runs=[_ok()], state="MERGED"),
    )
    result = _run(state, "cid-x", EnumJ8Want.ANY, sink)
    assert result.status is EnumJ8LiveStatus.NO_SUBJECT


def test_a_pinned_head_is_the_one_judged() -> None:
    sink = InMemoryEmissionSink()
    state = _state(omnimarket_11=_good(), omnimarket_12=_held_missing())
    result = _run(
        state,
        "cid-pin",
        EnumJ8Want.ANY,
        sink,
        pr="OmniNode-ai/omnimarket#12",
    )
    assert result.pr == "OmniNode-ai/omnimarket#12"
    assert result.verdict is EnumLabCheckVerdict.MISSING


# -------------------------------------------------------------------------------------------------- refusals


def test_error_a_stale_watcher_state_is_refused_not_judged() -> None:
    state = _state(omnimarket_11=_good())
    state["last_tick"] = (NOW - timedelta(hours=3)).isoformat()
    with pytest.raises(WatcherStateError, match="past the 900 s bound"):
        WatcherLabCheckCollector(state, _overlay(), NOW)


def test_error_a_state_with_no_pr_table_is_refused() -> None:
    with pytest.raises(WatcherStateError, match="no prs table"):
        WatcherLabCheckCollector({"last_tick": NOW.isoformat()}, _overlay(), NOW)


def test_error_an_injected_break_is_refused_under_an_overlay_that_forbids_it() -> None:
    sink = InMemoryEmissionSink()
    state = _state(omnimarket_12=_held_missing())
    result = _run(
        state,
        "cid-no",
        EnumJ8Want.ERROR,
        sink,
        inject=EnumJ8Break.BLIND_DECISION,
        overlay=_overlay(allow_injected_break=False),
    )
    assert result.status is EnumJ8LiveStatus.FAILED
    assert sink.events == []


# -------------------------------------------------------------------------------------------------- failing controls


def test_control_blind_decision_breaks_the_join() -> None:
    state = _state(omnimarket_12=_held_missing())
    clean = InMemoryEmissionSink()
    _run(state, "cid-c", EnumJ8Want.ERROR, clean)
    assert _fold(clean, NOW + timedelta(minutes=1)) == ("error_only", [])

    broken = InMemoryEmissionSink()
    result = _run(
        state,
        "cid-c",
        EnumJ8Want.ANY,
        broken,
        inject=EnumJ8Break.BLIND_DECISION,
    )
    assert result.injected is EnumJ8Break.BLIND_DECISION
    state_read, findings = _fold(broken, NOW + timedelta(minutes=1))
    assert state_read == "broken"
    assert findings == ["downstream_started_after_upstream_failed"]


def test_control_silent_landing_breaks_the_join_past_the_deadline() -> None:
    state = _state(omnimarket_12=_held_missing())
    sink = InMemoryEmissionSink()
    result = _run(
        state, "cid-s", EnumJ8Want.ANY, sink, inject=EnumJ8Break.SILENT_LANDING
    )
    assert result.status is EnumJ8LiveStatus.COMPLETED
    assert _fold(sink, NOW + timedelta(seconds=5))[0] == "not_run"
    late = _fold(sink, NOW + timedelta(seconds=CHAIN_DEADLINE_S + 60))
    assert late == ("broken", ["downstream_missing_typed_failure"])


def test_control_an_injected_break_has_no_head_to_hide_behind_when_every_check_passes() -> (
    None
):
    sink = InMemoryEmissionSink()
    result = _run(
        _state(omnimarket_11=_good()),
        "cid-n",
        EnumJ8Want.ANY,
        sink,
        inject=EnumJ8Break.BLIND_DECISION,
    )
    assert result.status is EnumJ8LiveStatus.NO_SUBJECT
    assert sink.events == []


# -------------------------------------------------------------------------------------------------- the runtime verb


def _install_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **over: Any
) -> Path:
    journal = tmp_path / "emissions.jsonl"
    watcher = tmp_path / "state.json"
    root = tmp_path / "overlays" / "node_pr_landing_join_j8"
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    fresh = {
        "last_tick": now.isoformat(),
        "prs": {
            "omnimarket#11": _good(),
            "omnimarket#12": _held_missing(),
        },
    }
    watcher.write_text(json.dumps(fresh))
    values = {
        "watcher_state_path": str(watcher),
        "watcher_max_age_seconds": 900,
        "owner": "OmniNode-ai",
        "lab_check_name": CHECK,
        "lab_bound_repos": ["omnimarket"],
        "max_candidates": 20,
        "poster_node": "node_lab_check_poster",
        "poster_failure_code": "lab_check_not_passed",
        "emission_journal": str(journal),
        "control_journal": str(tmp_path / "controls.jsonl"),
        "drill": False,
        "allow_injected_break": True,
        **over,
    }
    (root / "overlay.yaml").write_text(json.dumps(values))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    return journal


def test_the_runtime_verb_writes_both_chains_to_the_journal_and_the_fold_reads_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch)
    for want in ("golden", "error"):
        out = io.StringIO()
        with redirect_stdout(out):
            code = runtime_join_j8.main(["run", "--want", want])
        assert code == 0
        assert json.loads(out.getvalue())["status"] == "completed"
    events = JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    assert {e.piece for e in events} == {8, 9}
    sink = InMemoryEmissionSink()
    sink.events.extend(events)
    assert _fold(sink, datetime.now(UTC) + timedelta(seconds=30)) == ("passed", [])


def test_the_runtime_verb_refuses_a_stale_state_with_exit_three_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch, watcher_max_age_seconds=1)
    state = json.loads(Path(tmp_path / "state.json").read_text())
    state["last_tick"] = "2026-01-01T00:00:00Z"
    Path(tmp_path / "state.json").write_text(json.dumps(state))
    assert runtime_join_j8.main(["run", "--want", "any"]) == 3
    assert not journal.exists()


def test_the_runtime_verb_runs_a_named_break_that_reads_broken_in_the_control_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch)
    control = tmp_path / "controls.jsonl"
    out = io.StringIO()
    with redirect_stdout(out):
        code = runtime_join_j8.main(["run", "--inject", "blind-decision"])
    assert code == 0
    sink = InMemoryEmissionSink()
    sink.events.extend(
        JsonlEmissionSink(control).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert _fold(sink, datetime.now(UTC) + timedelta(seconds=30)) == (
        "broken",
        ["downstream_started_after_upstream_failed"],
    )
    assert not journal.exists()


def test_control_silent_landing_run_leaves_the_floors_journal_unread_as_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-23084: a named break run against the real overlay once left piece 8 failed and piece 9 silent in the floor's
    own journal, so the floor read J8 broken (downstream_missing_typed_failure) on a chain that was a control."""
    journal = _install_overlay(tmp_path, monkeypatch)
    for want in ("golden", "error"):
        with redirect_stdout(io.StringIO()):
            assert runtime_join_j8.main(["run", "--want", want]) == 0
    with redirect_stdout(io.StringIO()):
        assert runtime_join_j8.main(["run", "--inject", "silent-landing"]) == 0
    floor = InMemoryEmissionSink()
    floor.events.extend(
        JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    late = datetime.now(UTC) + timedelta(seconds=CHAIN_DEADLINE_S + 60)
    assert _fold(floor, late) == ("passed", [])
    control = InMemoryEmissionSink()
    control.events.extend(
        JsonlEmissionSink(tmp_path / "controls.jsonl").read(
            datetime(2000, 1, 1, tzinfo=UTC)
        )
    )
    assert _fold(control, late) == ("broken", ["downstream_missing_typed_failure"])


def test_error_an_injected_break_without_a_control_journal_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-23087: an overlay with no control_journal (the one installed before the split) never lets a control reach the
    floor's journal."""
    journal = _install_overlay(tmp_path, monkeypatch, control_journal=None)
    with redirect_stdout(io.StringIO()):
        assert runtime_join_j8.main(["run", "--inject", "silent-landing"]) == 3
    assert not journal.exists()
    assert not (tmp_path / "controls.jsonl").exists()


def _silent_landing_cid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        assert runtime_join_j8.main(["run", "--inject", "silent-landing"]) == 0
    return str(json.loads(out.getvalue())["correlation_id"])


def test_relocate_controls_moves_a_control_chain_out_of_the_floors_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor read J8 broken from control chains an earlier run wrote to its journal; moving them reads it passed."""
    journal = _install_overlay(tmp_path, monkeypatch)
    for want in ("golden", "error"):
        with redirect_stdout(io.StringIO()):
            assert runtime_join_j8.main(["run", "--want", want]) == 0
    cid = _silent_landing_cid(tmp_path, monkeypatch)
    control = tmp_path / "controls.jsonl"
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(control.read_text())
    control.unlink()
    late = datetime.now(UTC) + timedelta(seconds=CHAIN_DEADLINE_S + 60)
    floor = InMemoryEmissionSink()
    floor.events.extend(
        JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert _fold(floor, late) == ("broken", ["downstream_missing_typed_failure"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert runtime_join_j8.main(["relocate-controls", "--cid", cid]) == 0
    assert json.loads(out.getvalue()) == {"chains": 1, "events": 2}
    floor = InMemoryEmissionSink()
    floor.events.extend(
        JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert _fold(floor, late) == ("passed", [])
    moved = InMemoryEmissionSink()
    moved.events.extend(
        JsonlEmissionSink(control).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert _fold(moved, late) == ("broken", ["downstream_missing_typed_failure"])


def test_error_relocate_controls_refuses_a_real_chain_an_unknown_id_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch)
    out = io.StringIO()
    with redirect_stdout(out):
        assert runtime_join_j8.main(["run", "--want", "golden"]) == 0
    real = str(json.loads(out.getvalue())["correlation_id"])
    before = journal.read_text()
    for cid in (real, "0" * 32):
        with redirect_stdout(io.StringIO()):
            assert runtime_join_j8.main(["relocate-controls", "--cid", cid]) == 3
    assert journal.read_text() == before
    assert not (tmp_path / "controls.jsonl").exists()


def test_error_relocate_controls_without_a_control_journal_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch, control_journal=None)
    with redirect_stdout(io.StringIO()):
        assert runtime_join_j8.main(["run", "--want", "golden"]) == 0
    before = journal.read_text()
    with redirect_stdout(io.StringIO()):
        assert runtime_join_j8.main(["relocate-controls", "--cid", "x"]) == 3
    assert journal.read_text() == before


def test_error_an_overlay_whose_control_journal_is_the_floors_journal_is_refused() -> (
    None
):
    with pytest.raises(ValueError, match="control_journal must differ"):
        _overlay(control_journal="/unused/emissions.jsonl")


def test_the_unit_tick_runs_both_ends_and_writes_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch)
    evidence = tmp_path / "evidence"
    out = io.StringIO()
    with redirect_stdout(out):
        code = runtime_join_j8.main(
            ["--omni-home", str(tmp_path), "--state-dir", str(evidence)]
        )
    assert code == 0
    written = json.loads((evidence / "last-run.json").read_text())
    assert written["exit_code"] == 0
    assert {k: v["status"] for k, v in written["ends"].items()} == {
        "golden": "completed",
        "error": "completed",
    }
    assert written["ends"]["golden"]["outcome"] == "mergeable"
    assert written["ends"]["error"]["outcome"] == "held_missing"
    sink = InMemoryEmissionSink()
    sink.events.extend(
        JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert _fold(sink, datetime.now(UTC) + timedelta(seconds=30)) == ("passed", [])


def test_error_the_unit_tick_over_a_stale_state_writes_exit_three_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _install_overlay(tmp_path, monkeypatch, watcher_max_age_seconds=1)
    state = json.loads((tmp_path / "state.json").read_text())
    state["last_tick"] = "2026-01-01T00:00:00Z"
    (tmp_path / "state.json").write_text(json.dumps(state))
    evidence = tmp_path / "evidence"
    code = runtime_join_j8.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(evidence)]
    )
    assert code == 3
    assert json.loads((evidence / "last-run.json").read_text())["exit_code"] == 3
    assert not journal.exists()
