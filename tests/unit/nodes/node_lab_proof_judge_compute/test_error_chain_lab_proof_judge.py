# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a missing deployment fact or a malformed request exits 3 INDETERMINATE and never answers a pass."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)
from omnimarket.nodes.node_lab_proof_judge_compute.runtime_lab_proof_judge import main

_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_proof_judge_compute/overlay.example.yaml"
)


def test_no_overlay_root_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert main(["select-tests"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "INDETERMINATE" in captured.err


def test_malformed_request_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"repo": "x"}'))
    assert main(["changed-path-live"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "INDETERMINATE" in captured.err


def test_unset_receipts_root_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LAB_PROOF_JUDGE_RECEIPTS_ROOT")
    request = (
        '{"repo": "OmniNode-ai/omnimarket", "pr_number": 1, "head": "'
        + "a" * 40
        + '", "changed_paths": ["x"], "base_changed_path_live": false}'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(request))
    assert main(["changed-path-live"]) == 3
    assert "LAB_PROOF_JUDGE_RECEIPTS_ROOT" in capsys.readouterr().err


def test_naive_finish_time_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"finished_at": "2026-11-10T23:00:00"}')
    )
    assert main(["retention"]) == 3


def test_regex_lacking_node_group_refused() -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
    data["not_applicable"]["repos"][0]["node_path_regex"] = (
        "^src/omnimarket/nodes/([^/]+)/"
    )
    with pytest.raises(ValueError, match="node"):
        ModelLabProofJudgeOverlay.model_validate(data)


def test_unknown_runs_by_refused() -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
    data["not_applicable"]["repos"][0]["entries"][0]["runs_by"] = "whenever"
    with pytest.raises(ValueError, match="runs_by"):
        ModelLabProofJudgeOverlay.model_validate(data)
