# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20718: a closer run that closes zero tickets is a working closer, not a failed run.

The closer exited 70 (ZERO_FLIP_WITH_CANDIDATES) on every run with no flips, so a working closer
read as broken. The node now returns a typed reading (exit code 0, the candidates and the held
reasons counted) and an alarm row. What counts as proof for closing a ticket is untouched: the
reading is made from the run summary after the acceptor and dod_verify have decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_completion_rules import (
    held_reason_counts,
    zero_flip_reading,
    zero_flip_row,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutZeroFlipReading,
    ModelRunSummary,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep import (
    main,
)

pytestmark = pytest.mark.unit

STAMP = "2026-11-08T22:00:00Z"


def summary(**overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "run_key": "2026-11-08T2045Z",
        "selected": ["OMN-1", "OMN-2"],
        "flipped": [],
        "open": [
            {"id": "OMN-1", "unmet": [{"reason": "binding refused (no-cwd)"}]},
            {"id": "OMN-2", "unmet": [{"reason": "binding refused (no-cwd)"}]},
        ],
        "refused_bindings": [{"refused": "no-cwd"}],
        "flip_refused": [{"id": "OMN-3", "detail": "guard"}],
        "held_for_amendment": ["OMN-4", "OMN-5"],
        "deferred_count": 7,
        "apply": True,
        "candidate_sources": {
            "OMN-1": ["current", "merged"],
            "OMN-2": ["merged"],
            "OMN-6": ["current"],
        },
    }
    run.update(overrides)
    return run


def test_golden_chain_zero_flip_run_reads_as_a_working_closer_with_reasons_counted() -> (
    None
):
    reading = zero_flip_reading(ModelRunSummary.model_validate(summary()), STAMP)
    assert reading is not None
    assert reading.exit_code == 0
    assert reading.candidate_count == 2
    assert reading.reason_counts == {
        "candidate-budget": 7,
        "binding-refused:no-cwd": 2,
        "held-for-amendment": 2,
        "done-flip-refused": 1,
    }
    row = zero_flip_row(reading)
    for expected in (
        "| STATUS |",
        "state=zero-flip-reading",
        "exit=0",
        "candidates=2",
        "held_reasons=",
        "not a failed run",
    ):
        assert expected in row


def test_golden_chain_completion_carries_the_reading_before_the_terminal() -> None:
    result = HandlerDodCloseoutSweep().handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "completion",
                "date": "2026-11-08",
                "clock_utc": STAMP,
                "ledger_rows": [],
                "run_summary": summary(),
            }
        )
    )
    assert result.zero_flip_reading is not None
    assert result.zero_flip_reading.exit_code == 0
    assert result.completion_rows is not None
    assert [r.split(" | ")[1] for r in result.completion_rows] == ["STATUS", "TERMINAL"]


def test_a_run_that_flipped_a_ticket_has_no_zero_flip_reading() -> None:
    # CONTROL: the reading is present only because nothing flipped.
    flipped = ModelRunSummary.model_validate(summary(flipped=["OMN-1"]))
    assert zero_flip_reading(flipped, STAMP) is None


def test_a_dry_run_flips_nothing_by_design_and_reads_no_zero_flip() -> None:
    dry = ModelRunSummary.model_validate(summary(apply=False))
    assert zero_flip_reading(dry, STAMP) is None


def test_the_reading_does_not_touch_what_counts_as_proof() -> None:
    # The same run summary, read twice: the open tickets stay open and nothing is added to
    # flipped; the counts only describe why they stayed open.
    parsed = ModelRunSummary.model_validate(summary())
    assert held_reason_counts(parsed)["binding-refused:no-cwd"] == 2
    assert parsed.flipped == []
    assert len(parsed.open) == 2


def test_error_chain_the_reading_cannot_carry_a_failing_exit() -> None:
    with pytest.raises(ValidationError):
        ModelDodCloseoutZeroFlipReading(
            lane="dod-closeout-sweep",
            run_key="r",
            observed_at=STAMP,
            exit_code=70,
            candidate_count=1,
            selected_count=0,
            open_count=0,
            held_count=0,
            deferred_count=0,
            reason_counts={},
            top_reason="x",
        )


def test_error_chain_a_summary_with_an_unusable_run_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRunSummary.model_validate(summary(run_key="has space|pipe"))


def test_runtime_zero_flip_exits_zero_and_writes_the_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "run.json"
    source.write_text(json.dumps(summary()))
    rows = tmp_path / "rows.txt"
    code = main(
        [
            "zero-flip",
            "--summary",
            str(source),
            "--stamp",
            STAMP,
            "--rows-file",
            str(rows),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["exit_code"] == 0
    assert printed["candidate_count"] == 2
    assert "state=zero-flip-reading" in rows.read_text()


def test_runtime_zero_flip_with_a_flip_writes_no_row(tmp_path: Path) -> None:
    source = tmp_path / "run.json"
    source.write_text(json.dumps(summary(flipped=["OMN-1"])))
    rows = tmp_path / "rows.txt"
    assert (
        main(
            [
                "zero-flip",
                "--summary",
                str(source),
                "--stamp",
                STAMP,
                "--rows-file",
                str(rows),
            ]
        )
        == 0
    )
    assert not rows.exists()


def test_runtime_zero_flip_refuses_an_unreadable_summary(tmp_path: Path) -> None:
    # An unreadable input is never a zero-flip: exit 2, not 0.
    missing = tmp_path / "absent.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert main(["zero-flip", "--summary", str(missing), "--stamp", STAMP]) == 2
    assert main(["zero-flip", "--summary", str(broken), "--stamp", STAMP]) == 2
