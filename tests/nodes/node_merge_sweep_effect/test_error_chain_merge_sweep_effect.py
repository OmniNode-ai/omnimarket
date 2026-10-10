# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: what the effect node does when a source or the runner fails (OMN-20676)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_effect.handlers import (
    HandlerMergeSweepLaneRun,
    HandlerMergeSweepLoadFacts,
)
from omnimarket.nodes.node_merge_sweep_effect.models import (
    ModelMergeSweepLaneRunRequest,
    ModelMergeSweepLoadRequest,
)
from omnimarket.nodes.node_merge_sweep_effect.protocols import (
    MergeSweepCommandOutcome,
    MergeSweepPortError,
)

NOW = "2026-10-09T12:01:00Z"


def _state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema": 1,
        "operator": "op",
        "repos": {"omnimarket": {"default_branch": "dev"}},
        "last_tick": "2026-10-09T12:00:00Z",
        "last_full_resync": "2026-10-09T11:30:00Z",
        "merges": {},
        "prs": {
            "omnimarket#1": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 1,
                    "title": "t",
                    "base": "dev",
                    "head_ref": "h",
                    "head_sha": "a" * 40,
                    "state": "OPEN",
                    "draft": False,
                    "labels": [],
                },
                "ci": None,
            }
        },
    }
    state.update(over)
    return state


def _load(tmp_path: Path, state: Any) -> Any:
    path = tmp_path / "state.json"
    path.write_text(state if isinstance(state, str) else json.dumps(state))
    (tmp_path / "ledger.md").write_text("")
    (tmp_path / "floors.json").write_text(json.dumps({"per_repo": {}}))
    return HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(path),
            ledger_path=str(tmp_path / "ledger.md"),
            floors_path=str(tmp_path / "floors.json"),
            now=NOW,
            load1=1.0,
            cpus=2,
        )
    )


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ({"schema": 2}, "schema"),
        (_state(operator=""), "read-source"),
        (_state(last_tick="2026-10-09T11:50:00Z"), "stale"),
        (_state(last_full_resync="2026-10-09T09:00:00Z"), "inventory stale"),
        (_state(last_tick="never"), "no complete tick"),
        (_state(prs={}), "zero open"),
        (_state(repos={"other": {"default_branch": "dev"}}), "no default branch"),
        ("{not json", "unreadable"),
    ],
)
def test_a_state_that_is_not_fresh_is_refused_and_nothing_is_read(
    tmp_path: Path, state: Any, why: str
) -> None:
    loaded = _load(tmp_path, state)
    assert not loaded.ok
    assert why in loaded.why
    assert loaded.facts is None


def test_a_missing_state_is_refused(tmp_path: Path) -> None:
    loaded = HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(tmp_path / "absent.json"),
            ledger_path=str(tmp_path / "ledger.md"),
            floors_path=str(tmp_path / "floors.json"),
            now=NOW,
        )
    )
    assert not loaded.ok
    assert "no state file" in loaded.why


def test_a_head_with_no_check_read_is_unread_not_green(tmp_path: Path) -> None:
    loaded = _load(tmp_path, _state())
    assert loaded.ok
    [pr] = loaded.facts.open_prs
    assert pr.runs is None
    assert "no check-run read at this head" in pr.runs_why


def test_unreadable_floors_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_state()))
    (tmp_path / "floors.json").write_text("{}")
    loaded = HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(path),
            ledger_path=str(tmp_path / "ledger.md"),
            floors_path=str(tmp_path / "floors.json"),
            now=NOW,
        )
    )
    assert not loaded.ok
    assert "floors or ledger unreadable" in loaded.why


def test_an_unparseable_tick_is_an_unknown_tick(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks.jsonl"
    ticks.write_text('{"status": "OK"}\nnot json\n')
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_state()))
    (tmp_path / "ledger.md").write_text("")
    (tmp_path / "floors.json").write_text(json.dumps({"per_repo": {}}))
    loaded = HandlerMergeSweepLoadFacts().handle(
        ModelMergeSweepLoadRequest(
            state_path=str(path),
            ledger_path=str(tmp_path / "ledger.md"),
            ticks_path=str(ticks),
            floors_path=str(tmp_path / "floors.json"),
            now=NOW,
        )
    )
    assert loaded.facts is not None
    assert loaded.facts.ticks == [
        {"status": "OK"},
        {"status": "UNKNOWN", "refusal": "unparseable tick"},
    ]


class _ScriptedRunner:
    def __init__(self, outcomes: list[MergeSweepCommandOutcome | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, argv: Any, timeout_s: float) -> MergeSweepCommandOutcome:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _NoFiles:
    def __init__(
        self, receipt: dict[str, Any] | None = None, fail: bool = False
    ) -> None:
        self.receipt = receipt
        self.fail = fail

    def write_text(self, path: str, text: str) -> None:
        if self.fail:
            raise MergeSweepPortError("disk full")

    def read_json(self, path: str) -> dict[str, Any] | None:
        return self.receipt


def _lane() -> ModelMergeSweepLaneRunRequest:
    return ModelMergeSweepLaneRunRequest(
        runner_script="/s/r.py",
        brief_path="/tmp/b.md",
        brief_text="b",
        lane="l",
        model="sonnet",
        parent="p",
        ticket="OMN-1",
        max_waits=3,
    )


DETACHED = MergeSweepCommandOutcome(0, "DETACHED lane=l pid=1 receipt=/r.json\n", "")


def test_a_runner_that_cannot_start_the_lane_returns_start_failed() -> None:
    runner = _ScriptedRunner([MergeSweepCommandOutcome(75, "", "no host\n")])
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles()).handle(_lane())
    assert (result.started, result.exit_code, result.status) == (
        False,
        75,
        "start-failed",
    )
    assert "no host" in result.result


def test_a_runner_that_prints_no_receipt_returns_start_failed() -> None:
    runner = _ScriptedRunner([MergeSweepCommandOutcome(0, "started\n", "")])
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles()).handle(_lane())
    assert (result.started, result.status) == (False, "start-failed")


def test_a_port_failure_is_a_typed_result() -> None:
    runner = _ScriptedRunner([MergeSweepPortError("no python3")])
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles()).handle(_lane())
    assert (result.started, result.exit_code, result.status) == (
        False,
        1,
        "start-failed",
    )
    assert "no python3" in result.result
    unwritable = HandlerMergeSweepLaneRun(
        runner=_ScriptedRunner([]), files=_NoFiles(fail=True)
    ).handle(_lane())
    assert "disk full" in unwritable.result


def test_a_lane_still_running_after_the_waits_is_not_a_receipt() -> None:
    still = MergeSweepCommandOutcome(3, "", "")
    runner = _ScriptedRunner([DETACHED, still, still, still])
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles()).handle(_lane())
    assert (result.started, result.exit_code, result.status) == (
        True,
        3,
        "wait-exhausted",
    )
    assert result.waits == 3


def test_an_unreadable_receipt_is_named() -> None:
    runner = _ScriptedRunner([DETACHED, MergeSweepCommandOutcome(0, "", "")])
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles(None)).handle(
        _lane()
    )
    assert (result.exit_code, result.status) == (0, "receipt-unreadable")


def test_the_receipt_exit_code_is_the_lane_result() -> None:
    runner = _ScriptedRunner([DETACHED, MergeSweepCommandOutcome(1, "", "")])
    receipt = {"exit_code": 77, "status": "host-limited", "host": "h201"}
    result = HandlerMergeSweepLaneRun(runner=runner, files=_NoFiles(receipt)).handle(
        _lane()
    )
    assert (result.exit_code, result.status, result.host) == (
        77,
        "host-limited",
        "h201",
    )
