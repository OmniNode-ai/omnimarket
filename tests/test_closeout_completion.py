# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's completion receipt and morning health row, decided by the node (OMN-20675).

node_dod_closeout_sweep_compute holds what the standalone closeout-completion module
decided: the completed-run receipts read from the ledger rows the caller passes (archive
rolls and live ledger as one history, a run key counted once), the zero-flip streak, the
top deferral reason, the FRICTION row on the third consecutive zero and the TERMINAL that
names it, and the GREEN/RED/UNKNOWN row the morning report shows. The caller reads the
ledger and appends the rows; the node reads nothing and writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_dod_closeout_sweep_compute as node_package
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloserHealth,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
HANDLER = HandlerDodCloseoutSweep()
DATE = "2026-10-06"
STAMP = "2026-10-06T03:00:00Z"
COMMON = "lane=dod-closeout-sweep | run=run-3 | actor=launchd:dod-closeout-sweep"
COST = "cost=est ~1.5 lane-hours (this run's declared scheduled closer budget)"


def receipt(run: int, flipped: int = 0) -> str:
    return (
        f"2026-10-06T{run:02d}:00:00Z | TERMINAL | lane=dod-closeout-sweep | "
        f"run=run-{run} | flipped={flipped} | friction=none"
    )


def summary(**overrides: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "run_key": "run-3",
        "selected": ["OMN-1", "OMN-2"],
        "flipped": [],
        "deferred_count": 57,
        "held_for_amendment": [],
        "open": [{"id": "OMN-1", "unmet": [{"reason": "binding refused (no-cwd)"}]}],
        "refused_bindings": [{"refused": "no-cwd"}, {"refused": "no-cwd"}],
    }
    run.update(overrides)
    return run


def complete(
    rows: list[str], run: dict[str, Any] | None = None, **fields: Any
) -> ModelDodCloseoutDecisionResult:
    request = {
        "kind": "completion",
        "date": DATE,
        "clock_utc": STAMP,
        "ledger_rows": rows,
        "run_summary": summary() if run is None else run,
        **fields,
    }
    return HANDLER.handle(ModelDodCloseoutDecisionRequest.model_validate(request))


def ground_state(rows: list[str]) -> ModelDodCloseoutDecisionResult:
    return HANDLER.handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {"kind": "ground_state", "date": DATE, "ledger_rows": rows}
        )
    )


def test_third_zero_flip_completion_emits_friction_then_the_terminal_naming_it() -> (
    None
):
    result = complete([receipt(1), receipt(2)])
    assert result.zero_flip_streak == 3
    assert result.top_deferral_reason == "binding-refused:no-cwd"
    assert result.completion_rows == [
        f"{STAMP} | FRICTION | {COMMON} | existing=OMN-20655 | class=zero-flip-streak"
        " | zero_flip_streak=3 | top_deferral_reason=binding-refused:no-cwd | " + COST,
        f"{STAMP} | TERMINAL | {COMMON} | ticket=OMN-17427 | worktree=none | selected=2"
        " | flipped=0 | open=1 | deferred=57 | held=0 | zero_flip_streak=3"
        " | top_deferral_reason=binding-refused:no-cwd"
        f" | friction={STAMP} lane=dod-closeout-sweep | no report: nothing changed",
    ]


@pytest.mark.parametrize("history", [[receipt(1)], [receipt(1), receipt(2, 1)]])
def test_a_short_or_reset_streak_writes_only_the_terminal(history: list[str]) -> None:
    result = complete(history)
    assert result.completion_rows is not None
    assert len(result.completion_rows) == 1
    assert "| TERMINAL |" in result.completion_rows[0]
    assert "friction=none" in result.completion_rows[0]


def test_a_flip_resets_the_streak_to_zero() -> None:
    result = complete([receipt(1), receipt(2)], summary(flipped=["OMN-1"]))
    assert result.zero_flip_streak == 0
    assert result.completion_rows is not None
    assert len(result.completion_rows) == 1
    assert "flipped=1" in result.completion_rows[0]
    assert "zero_flip_streak=0" in result.completion_rows[0]


def test_a_force_replay_does_not_count_its_own_prior_terminal() -> None:
    result = complete([receipt(1), receipt(2), receipt(3)])
    assert result.zero_flip_streak == 3


def test_the_report_is_cited_as_one_cell() -> None:
    result = complete([], report_ref="beta/tracking/a | b.md\nnext")
    assert result.completion_rows is not None
    assert result.completion_rows[-1].endswith("| beta/tracking/a / b.md next")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "binding-refused:no-cwd"),
        (
            {"open": [{"id": "OMN-1", "unmet": [{"reason": "no verdict | here"}]}]},
            "no verdict / here",
        ),
        ({"open": []}, "binding-refused:no-cwd"),
        (
            {"open": [], "refused_bindings": [], "flip_refused": [{"id": "OMN-1"}]},
            "done-flip-refused",
        ),
        (
            {"open": [], "refused_bindings": [], "held_for_amendment": ["OMN-9"]},
            "held-for-amendment",
        ),
        ({"open": [], "refused_bindings": []}, "candidate-budget"),
        (
            {"open": [], "refused_bindings": [], "deferred_count": 0, "selected": []},
            "no-candidates",
        ),
        ({"open": [], "refused_bindings": [], "deferred_count": 0}, "no-applied-flip"),
    ],
)
def test_the_top_deferral_reason_counts_unmet_criteria_then_falls_back(
    overrides: dict[str, Any], reason: str
) -> None:
    assert complete([], summary(**overrides)).top_deferral_reason == reason


def test_ties_in_the_top_reason_break_alphabetically() -> None:
    run = summary(
        open=[
            {"id": "OMN-1", "unmet": [{"reason": "zeta"}, {"reason": "alpha"}]},
            {"id": "OMN-2", "unmet": [{"reason": "binding refused (no-cwd)"}]},
        ]
    )
    assert complete([], run).top_deferral_reason == "alpha"


def test_ground_state_reads_three_receipts_across_an_archive_roll() -> None:
    archive = [receipt(1), receipt(2)]
    live = [receipt(2), receipt(3)]  # a mirrored duplicate is one run
    red = ground_state(archive + live)
    assert red.health is EnumCloserHealth.RED
    assert red.zero_flip_streak == 3
    assert red.ground_state_row == (
        "| dod-closeout-sweep | RED | zero-flip streak=3; top deferral reason=unknown;"
        " last run=run-3 |"
    )
    green = ground_state([receipt(3), receipt(4, 1)])
    assert green.health is EnumCloserHealth.GREEN
    assert green.ground_state_row is not None
    assert "zero-flip streak=0" in green.ground_state_row


def test_ground_state_shows_the_latest_receipts_deferral_reason() -> None:
    rows = [
        receipt(1),
        "2026-10-06T02:00:00Z | TERMINAL | lane=dod-closeout-sweep | run=run-2"
        " | flipped=0 | top_deferral_reason=binding-refused:no-cwd | friction=none",
    ]
    result = ground_state(rows)
    assert result.top_deferral_reason == "binding-refused:no-cwd"
    assert result.health is EnumCloserHealth.GREEN


def test_no_completed_receipt_is_unknown_never_green() -> None:
    result = ground_state(["# a heading", "not a row"])
    assert result.health is EnumCloserHealth.UNKNOWN
    assert (
        result.ground_state_row
        == "| dod-closeout-sweep | UNKNOWN | no completed receipts |"
    )


def test_claims_other_lanes_unkeyed_and_shared_cell_rows_are_not_receipts() -> None:
    rows = [
        "2026-10-06T01:00:00Z | CLAIM | lane=dod-closeout-sweep | run=run-1 | flipped=0",
        "2026-10-06T01:00:00Z | TERMINAL | lane=other-lane | run=run-1 | flipped=0",
        "2026-10-06T01:00:00Z | TERMINAL | lane=dod-closeout-sweep | flipped=0",
        "2026-10-06T01:00:00Z | TERMINAL | lane=dod-closeout-sweep actor=x | run=r | flipped=0",
    ]
    assert ground_state(rows).health is EnumCloserHealth.UNKNOWN


@pytest.mark.parametrize("cell", ["flipped=unknown", "friction=none"])
def test_a_malformed_receipt_is_refused_instead_of_claiming_green(cell: str) -> None:
    row = receipt(1).replace("flipped=0", cell)
    with pytest.raises(ValueError, match="malformed closer completion"):
        ground_state([row])
    with pytest.raises(ValueError, match="malformed closer completion"):
        complete([row])


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"clock_utc": "2026-10-06 03:00"}, "clock_utc must be"),
        ({"run_summary": summary(run_key="run 3")}, "run_key"),
        ({"run_summary": summary(run_key="")}, "run_key"),
        ({"run_summary": {"run_key": "run-3"}}, "selected"),
        ({"run_summary": None}, "completion requires run_summary"),
        ({"ledger_rows": None}, "completion requires ledger_rows"),
        ({"force": True}, "completion does not take force"),
    ],
)
def test_error_chain_refuses_an_invalid_completion_before_the_handler_runs(
    fields: dict[str, Any], message: str
) -> None:
    request = {
        "kind": "completion",
        "date": DATE,
        "clock_utc": STAMP,
        "ledger_rows": [],
        "run_summary": summary(),
        **fields,
    }
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(request)


def test_ground_state_takes_only_the_ledger_rows() -> None:
    with pytest.raises(ValidationError, match="ground_state does not take run_summary"):
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "ground_state",
                "date": DATE,
                "ledger_rows": [],
                "run_summary": summary(),
            }
        )


# --- golden chain and error chain over the in-memory bus -------------------------


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_completion_then_ground_state_over_the_bus(
    tmp_path: Path,
) -> None:
    history = [receipt(1), receipt(2)]
    done = (
        await _run(
            tmp_path / "completion",
            {
                "kind": "completion",
                "date": DATE,
                "clock_utc": STAMP,
                "ledger_rows": history,
                "run_summary": summary(delegations=[], sprint="ignored"),
            },
        )
    ).handler_result
    assert isinstance(done, ModelDodCloseoutDecisionResult)
    assert done.completion_rows is not None
    assert [r.split(" | ")[1] for r in done.completion_rows] == ["FRICTION", "TERMINAL"]

    morning = (
        await _run(
            tmp_path / "ground",
            {
                "kind": "ground_state",
                "date": DATE,
                "ledger_rows": [*history, *done.completion_rows],
            },
        )
    ).handler_result
    assert isinstance(morning, ModelDodCloseoutDecisionResult)
    assert morning.health is EnumCloserHealth.RED
    assert morning.ground_state_row == (
        "| dod-closeout-sweep | RED | zero-flip streak=3;"
        " top deferral reason=binding-refused:no-cwd; last run=run-3 |"
    )
    assert (
        ModelDodCloseoutDecisionResult.model_validate_json(morning.model_dump_json())
        == morning
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_completion(
    tmp_path: Path,
) -> None:
    invalid = await _run(
        tmp_path / "invalid", {"kind": "completion", "date": DATE, "ledger_rows": []}
    )
    assert invalid.handler_result is None
    refused_dir = tmp_path / "refused"
    refused_dir.mkdir()
    input_path = refused_dir / "request.json"
    input_path.write_text(
        json.dumps(
            {
                "kind": "ground_state",
                "date": DATE,
                "ledger_rows": [receipt(1).replace("flipped=0", "flipped=x")],
            }
        )
    )
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=refused_dir / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
