# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests of the fence: each fails when either side of a seam changes shape (OMN-24248).

Seams: (1) the ledger rows the lane runner and the lanes write -> the fence's CLAIM and TERMINAL
pairing; (2) the lane runner's receipt file -> the running-lane read; (3) the fence report JSON ->
the lab fill effect node's FencesTool, which reads its ``fenced_tickets`` list. The fence runs as a
process; no topic crosses these seams.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_enumerate_facts import (
    FencesTool,
)
from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)
from omnimarket.nodes.node_triage_fences_compute.models import (
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesVerdict,
)
from omnimarket.nodes.node_triage_fences_effect.handlers.handler_triage_fences_receipts import (
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


# ---- seam 3: the fence report -> the lab fill effect node's FencesTool ------------------------


def test_the_lab_fill_reads_the_fenced_tickets_the_node_prints(tmp_path: Path) -> None:
    root = tmp_path / "overlay-root"
    (root / "node_triage_fences_effect").mkdir(parents=True)
    receipts = tmp_path / "receipts"
    _runner_receipt(receipts, started_at="2026-11-13T14:00:00Z")
    overlay = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    overlay["receipt_dir"] = str(receipts)
    (root / "node_triage_fences_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(overlay), encoding="utf-8"
    )
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text(
        "2026-11-13T09:00:00Z | CLAIM | lane=lane-a | ticket=OMN-90001 | actor=script:t | x\n"
        "2026-11-13T09:00:00Z | CLAIM | lane=dead-lane | ticket=OMN-90002 | actor=script:t | x\n",
        encoding="utf-8",
    )
    argv = (sys.executable, "-m", CLI, str(ledger), "--now", "2026-11-13T15:00:00Z")
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    fenced = FencesTool(argv=argv, env=env)(60.0)
    printed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env},
    )
    assert fenced == tuple(json.loads(printed.stdout)["fenced_tickets"])
    assert fenced == (
        "OMN-90001",
    )  # lane-a runs by its receipt; dead-lane fences nothing
