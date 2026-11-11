# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the pool driver's command records a node's own run, then the judge reads the measured case NOT_APPLICABLE.

The chain runs through the CLI exactly as the driver calls it: record-substitute (the node's own run on a lab host, bound
to the head), then changed-path-live with the change's facts on stdin, then retention for the proof's log.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_proof_judge_compute.runtime_lab_proof_judge import main

HEAD = "a8f3c7b1b73133f0063f6c6fb07eaf90e94a8d36"
REPO = "OmniNode-ai/omnimarket"
REQUEST = {
    "repo": REPO,
    "pr_number": 5689,
    "head": HEAD,
    "changed_paths": [
        "src/omnimarket/nodes/node_overlay_drift_effect/models/model_overlay_drift.py",
        "src/omnimarket/nodes/node_overlay_merge_order_compute/handlers/handler_overlay_merge_order.py",
        "src/omnimarket/enums/enum_overlay_verdict.py",
        "tests/unit/nodes/node_overlay_merge_order_compute/test_check_entry.py",
    ],
    "base_changed_path_live": False,
    "contract_local_nodes": ["node_overlay_merge_order_compute"],
}


def _call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    stdin: str = "",
) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(argv)
    return rc, capsys.readouterr().out


def _record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    head: str,
    exit_code: int = 0,
) -> None:
    rc, out = _call(
        monkeypatch,
        capsys,
        [
            "record-substitute",
            "--repo",
            REPO,
            "--node",
            "node_overlay_drift_effect",
            "--head",
            head,
            "--host",
            "labhost",
            "--host-class",
            "lab",
            "--command",
            "node_overlay_drift_effect run --no-write",
            "--exit-code",
            str(exit_code),
            "--log-file",
            "/logs/drift.log",
            "--finished-at",
            "2026-11-10T23:00:00+00:00",
        ],
    )
    assert rc == 0
    assert out.strip().endswith(".receipt.json")


def test_golden_measured_case_reads_not_applicable(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, HEAD)
    rc, out = _call(monkeypatch, capsys, ["changed-path-live"], json.dumps(REQUEST))
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "NOT_APPLICABLE"
    assert answer["ok"] is True
    assert answer["class_id"] == "runs_outside_runtime"
    assert [e["node"] for e in answer["matched_entries"]] == [
        "node_overlay_drift_effect"
    ]
    assert answer["substitute_proofs"][0]["host"] == "labhost"
    assert answer["use"]["outcome"] == "not_applicable"


def test_golden_same_change_without_substitute_proof_fails(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _call(monkeypatch, capsys, ["changed-path-live"], json.dumps(REQUEST))
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "FAIL"
    assert answer["ok"] is False
    assert answer["missing_proof_nodes"] == ["node_overlay_drift_effect"]
    assert answer["use"]["outcome"] == "refused_no_substitute"


def test_golden_receipt_for_another_head_does_not_count(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, "b" * 40)
    _, out = _call(monkeypatch, capsys, ["changed-path-live"], json.dumps(REQUEST))
    assert json.loads(out)["verdict"] == "FAIL"


def test_golden_retention_and_selection(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _call(
        monkeypatch,
        capsys,
        ["retention"],
        json.dumps({"finished_at": "2026-11-10T23:00:00+00:00"}),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["log_hours"] == 24
    assert answer["retain_until"].startswith("2026-11-11T23:00:00")
    rc, out = _call(
        monkeypatch,
        capsys,
        ["select-tests"],
        json.dumps({"repo": REPO, "broker_available": False}),
    )
    assert json.loads(out)["marker_expression"] == "not kafka"
    rc, out = _call(
        monkeypatch,
        capsys,
        ["select-tests"],
        json.dumps({"repo": REPO, "broker_available": True}),
    )
    assert json.loads(out)["marker_expression"] == ""
