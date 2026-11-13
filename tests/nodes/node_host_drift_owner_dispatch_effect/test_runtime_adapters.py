# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime adapters of the host drift owner dispatch read real files and a real subprocess (OMN-24239)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    FileDispatchRecord,
    FileDriftOpenItems,
    LedgerQueryLiveLanes,
    SubprocessLanePlacer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    LiveLanesUnreadableError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 16, 0, 0, tzinfo=UTC)
CAUSE = "host-drift:h-example:env-file:host-env"

# The layout the host drift checker writes (read from a lab host 2026-11-13): host, last_check_at, open[], generation, outbox.
STATE = {
    "host": "h-example",
    "last_check_at": "2026-11-13T15:30:54Z",
    "open": [
        {
            "key": "env-file:host-env",
            "kind": "env-file",
            "declared": "no secret name beyond the store bootstrap names",
            "observed": "a file holds 1 secret names",
            "first_seen_at": "2026-11-07T00:01:37Z",
            "bound_seconds": 3600,
            "verdict": "DRIFT",
        }
    ],
    "generation": 1120,
    "outbox": [],
}


def test_the_checkers_state_file_is_read_with_its_host(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(STATE), encoding="utf-8")
    state = FileDriftOpenItems(path).read()
    assert state.host == "h-example"
    assert [(i.host, i.key, i.first_seen_at) for i in state.items] == [
        ("h-example", "env-file:host-env", "2026-11-07T00:01:37Z")
    ]


def test_the_dispatch_record_round_trips_and_is_empty_when_absent(
    tmp_path: Path,
) -> None:
    record = FileDispatchRecord(tmp_path / "sub" / "dispatched.json")
    assert record.load() == {}
    record.save({CAUSE: "2026-11-13T16:00:00Z"})
    assert record.load() == {CAUSE: "2026-11-13T16:00:00Z"}


def _ledger(
    rows_by_kind: dict[str, list[dict[str, str]]],
) -> tuple[Callable[..., CompletedProcess[str]], list[list[str]]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(argv)
        kind = argv[argv.index("--kind") + 1]
        answer = {"rows": rows_by_kind.get(kind, []), "error": None, "refusal": None}
        return CompletedProcess(
            argv, 0, "[onex] warning\n" + json.dumps(answer) + "\n", ""
        )

    return run, calls


def _claim(lane: str) -> dict[str, str]:
    return {
        "row_lane": lane,
        "row_type": "CLAIM",
        "text": f"2026-11-13T10:00:00Z | CLAIM | lane={lane} | cause={CAUSE}",
    }


def _live(run: Callable[..., CompletedProcess[str]]) -> LedgerQueryLiveLanes:
    return LedgerQueryLiveLanes(
        ("onex-example", "ledger", "query"), 72, 50, 30, lambda: NOW, run
    )


def test_a_claim_with_no_terminal_is_a_live_lane() -> None:
    run, calls = _ledger({"CLAIM": [_claim("host-drift-x-20261113T1000Z")]})
    live = _live(run).read((CAUSE,))
    assert live.covered_causes == (CAUSE,)
    assert live.covered_lanes == ("host-drift-x-20261113T1000Z",)
    claim_call = calls[0]
    assert claim_call[claim_call.index("--term") + 1] == CAUSE
    assert claim_call[claim_call.index("--since") + 1] == "2026-11-10T16:00:00Z"


def test_a_claim_followed_by_a_terminal_is_not_live() -> None:
    run, _ = _ledger(
        {
            "CLAIM": [_claim("host-drift-x-20261113T1000Z")],
            "TERMINAL": [
                {
                    "row_lane": "host-drift-x-20261113T1000Z",
                    "row_type": "TERMINAL",
                    "text": "x",
                }
            ],
        }
    )
    live = _live(run).read((CAUSE,))
    assert live.covered_causes == ()
    assert live.covered_lanes == ()


def test_a_claim_that_does_not_name_the_cause_does_not_cover_it() -> None:
    other = {
        "row_lane": "l",
        "row_type": "CLAIM",
        "text": "2026-11-13T10:00:00Z | CLAIM | lane=l | cause=other",
    }
    run, _ = _ledger({"CLAIM": [other]})
    assert _live(run).read((CAUSE,)).covered_causes == ()


def test_a_failed_ledger_read_is_unreadable_never_no_lanes() -> None:
    def run(argv: list[str], **_: object) -> CompletedProcess[str]:
        return CompletedProcess(
            argv, 2, "", "Error: the ledger node refused the read\n"
        )

    with pytest.raises(
        LiveLanesUnreadableError,
        match=r"exited 2: Error: the ledger node refused the read",
    ):
        _live(run).read((CAUSE,))


def test_a_ledger_answer_with_a_refusal_is_unreadable() -> None:
    def run(argv: list[str], **_: object) -> CompletedProcess[str]:
        return CompletedProcess(
            argv, 0, json.dumps({"rows": [], "refusal": "too large"}), ""
        )

    with pytest.raises(LiveLanesUnreadableError, match="too large"):
        _live(run).read((CAUSE,))


def _argv(code: str, tmp: Path) -> tuple[str, ...]:
    return (sys.executable, "-c", code, "--lane", "lane-x")


def test_a_runner_that_exits_zero_started_and_one_that_exits_nonzero_did_not(
    tmp_path: Path,
) -> None:
    runner = SubprocessLanePlacer(20, tmp_path / "logs")
    assert runner.place(_argv("raise SystemExit(0)", tmp_path)).started is True
    failed = runner.place(
        _argv("import sys; print('no placement'); sys.exit(3)", tmp_path)
    )
    assert failed.started is False
    assert failed.detail == "runner exited 3: no placement"


def test_a_runner_still_running_past_the_settle_window_started(tmp_path: Path) -> None:
    outcome = SubprocessLanePlacer(0.2, tmp_path / "logs").place(
        _argv("import time; time.sleep(3)", tmp_path)
    )
    assert outcome.started is True


def test_a_runner_that_cannot_be_executed_did_not_start(tmp_path: Path) -> None:
    outcome = SubprocessLanePlacer(1, tmp_path / "logs").place(
        ("/no/such/interpreter", "--lane", "lane-x")
    )
    assert outcome.started is False
    assert outcome.detail.startswith("dispatch failed")
