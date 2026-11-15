# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests of the fence: each fails when either side of a seam changes shape (OMN-24248).

Seams: (1) the ledger rows the lane runner and the lanes write -> the fence's CLAIM and TERMINAL
pairing; (2) the lane runner's receipt file -> the running-lane read. The fence runs as a
process; no topic crosses these seams.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.triage_fences import (
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesVerdict,
)
from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)
from omnimarket.nodes.node_triage_fences_effect.logic.triage_fences_receipts import (
    running_lanes,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parents[1] / "overlay.example.yaml"
CLI = "omnimarket.nodes.node_triage_fences_effect.runtime_triage_fences_cli"


def _derive(
    text: str, now: str, running: tuple[str, ...] = ()
) -> ModelTriageFencesVerdict:
    return HandlerTriageFencesDerive().handle(
        ModelTriageFencesDeriveRequest(
            ledger_name="seam.md",
            ledger_text=text,
            now=datetime.fromisoformat(now).astimezone(UTC),
            stale_after_hours=0.75,
            pr_window_hours=6.0,
            running_lanes=running,
        )
    )


# ---- seam 1: ledger writers -> the fence ------------------------------------------------------
# Two rows of lane closer-fix-6764 taken verbatim from the live ledger of 2026-11-13: the child
# CLAIM the lane runner wrote (it carries parent_ticket) and the lane's own TERMINAL, which names
# the carrier ticket only. The third row is the TERMINAL written to close the stranded child claim.


def _rows() -> str:
    return (FIXTURES / "ledger_seam_rows.md").read_text(encoding="utf-8")


def test_the_real_child_claim_and_lane_terminal_leave_nothing_fenced() -> None:
    assert _derive(_rows(), "2026-11-13T12:00:00+00:00").fenced_tickets == ()


def test_seam_control_a_renamed_parent_cell_strands_the_child_claim() -> None:
    """If the runner stopped writing parent_ticket, the child claim would fence again: red."""
    renamed = _rows().replace("parent_ticket=", "carrier_ticket=")
    verdict = _derive(renamed, "2026-11-13T12:00:00+00:00")
    assert verdict.fenced_tickets == ("OMN-24138",)


def test_the_closing_terminal_pairs_with_the_child_claim() -> None:
    claim_only = _rows().splitlines()[0]
    closing = (FIXTURES / "ledger_closing_row.md").read_text(encoding="utf-8")
    stranded = _derive(claim_only, "2026-11-13T11:00:00+00:00")
    assert stranded.fenced_tickets == ("OMN-24138",)
    closed = _derive(
        claim_only + "\n" + closing, "2026-11-13T17:00:00+00:00", ("closer-fix-6764",)
    )
    assert closed.fenced_tickets == ()
    assert closed.counts.open_claims == 0


# ---- seam 2: the runner's receipt -> the running-lane read ------------------------------------
# The keys are the ones the lane runner writes: status "running" with final false while the engine
# runs, and its outcome with final true after.


def _runner_receipt(folder: Path, **over: object) -> None:
    body = {
        "lane": "lane-a",
        "run_id": "rlane-lane-a-0a1b2c",
        "status": "running",
        "final": False,
        "started_at": "2026-11-13T14:00:00Z",
        "host": "h053",
        "pid": 1351275,
        "parent": "orchestrator-31d9",
        "ticket": "OMN-24248",
        "model": "sonnet",
        "log": "/state/lane-a/rlane-lane-a-0a1b2c.log",
        "receipt": "/state/lane-a/rlane-lane-a-0a1b2c.json",
    }
    body.update(over)
    (folder / "lane-a").mkdir(parents=True, exist_ok=True)
    (folder / "lane-a" / "rlane-lane-a-0a1b2c.json").write_text(
        json.dumps(body), encoding="utf-8"
    )


def test_a_runner_receipt_reads_running_then_ended(tmp_path: Path) -> None:
    now = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
    _runner_receipt(tmp_path)
    assert running_lanes(tmp_path, now, 240) == (("lane-a",), ())
    _runner_receipt(
        tmp_path, status="success", final=True, ended_at="2026-11-13T14:30:00Z"
    )
    assert running_lanes(tmp_path, now, 240) == ((), ())


def test_seam_control_a_renamed_status_key_reads_as_not_running(tmp_path: Path) -> None:
    """If the runner renamed ``status``, no lane would read running: the control goes red."""
    _runner_receipt(tmp_path)
    receipt = tmp_path / "lane-a" / "rlane-lane-a-0a1b2c.json"
    body = json.loads(receipt.read_text(encoding="utf-8"))
    body["state"] = body.pop("status")
    receipt.write_text(json.dumps(body), encoding="utf-8")
    assert running_lanes(tmp_path, datetime(2026, 11, 13, 15, 0, tzinfo=UTC), 240) == (
        (),
        (),
    )
