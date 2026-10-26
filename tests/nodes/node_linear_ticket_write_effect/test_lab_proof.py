# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The AC5 lab proof validator refuses the recorded ACL refusal and partial proofs."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_linear_ticket_write_effect.handlers import handler_lab_proof

pytestmark = pytest.mark.unit

RECORDED_PROOF = Path(__file__).parents[3] / "docs/operations/OMN-20595-lab.json"


def _receipt(run_id: str, offset: int) -> dict[str, object]:
    return {"run_id": run_id, "terminal_offset": offset}


def _complete_proof() -> dict[str, object]:
    return {
        "status": "complete",
        "remote_reads": [
            {
                "host": host,
                "ticket_id": "OMN-20570",
                "title": "Scratch title",
                "run_id": f"read-{host}",
                "remote_lane_receipt": f"receipts/{host}.json",
                "terminal_offset": index,
            }
            for index, host in enumerate(("h101", "h201", "h202"))
        ],
        "guard_refusal": {
            "run_id": "refusal-run",
            "finding_codes": ["unfalsified_criterion"],
        },
        "scratch": {
            "ticket_id": "OMN-99999",
            "create": _receipt("create-run", 10),
            "read": _receipt("read-run", 11),
            "comment": _receipt("comment-run", 12),
            "cancel": _receipt("cancel-run", 13),
            "final_state": "Canceled",
        },
    }


def test_complete_proof_passes() -> None:
    assert handler_lab_proof.verify(_complete_proof()) == []


def test_recorded_acl_refusal_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["handler_lab_proof", str(RECORDED_PROOF)])
    assert handler_lab_proof.main() == 1
    output = capsys.readouterr().out
    assert "lab proof is incomplete" in output
    for host in ("h101", "h201", "h202"):
        assert f"{host}: missing remote lane read receipt" in output
    assert "missing scratch ticket id" in output


@pytest.mark.parametrize("host", ["h101", "h201", "h202"])
def test_missing_host_read_is_refused(host: str) -> None:
    proof = _complete_proof()
    reads = proof["remote_reads"]
    assert isinstance(reads, list)
    proof["remote_reads"] = [r for r in reads if r["host"] != host]
    assert handler_lab_proof.verify(proof) == [
        f"{host}: missing remote lane read receipt naming OMN-20570 and its title"
    ]


def test_guard_refusal_without_unfalsified_criterion_is_refused() -> None:
    proof = _complete_proof()
    proof["guard_refusal"] = {"run_id": "refusal-run", "finding_codes": ["other"]}
    assert handler_lab_proof.verify(proof) == [
        "missing lab guard refusal with unfalsified_criterion"
    ]


@pytest.mark.parametrize("operation", ["create", "read", "comment", "cancel"])
def test_scratch_operation_without_terminal_is_refused(operation: str) -> None:
    proof = copy.deepcopy(_complete_proof())
    scratch = proof["scratch"]
    assert isinstance(scratch, dict)
    scratch[operation] = {"run_id": f"{operation}-run"}
    assert handler_lab_proof.verify(proof) == [
        f"scratch {operation}: missing bus terminal receipt"
    ]


def test_scratch_not_canceled_is_refused() -> None:
    proof = _complete_proof()
    scratch = proof["scratch"]
    assert isinstance(scratch, dict)
    scratch["final_state"] = "Todo"
    assert handler_lab_proof.verify(proof) == ["scratch ticket is not proven canceled"]


def test_unreadable_proof_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "proof.json"
    path.write_text(json.dumps([1, 2]))
    monkeypatch.setattr(sys, "argv", ["handler_lab_proof", str(path)])
    assert handler_lab_proof.main() == 1
    monkeypatch.setattr(sys, "argv", ["handler_lab_proof", str(tmp_path / "absent")])
    assert handler_lab_proof.main() == 1
