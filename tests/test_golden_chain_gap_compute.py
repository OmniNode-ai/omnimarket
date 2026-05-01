from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_gap_compute.handlers import handler_gap_compute
from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    HandlerGapCompute,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    EnumGapStatus,
    ModelGapComputeResult,
)


def _write_contract(path: Path, *, topic: str, stub: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "node_sample",
        "node_type": "compute",
        "terminal_event": topic,
        "event_bus": {
            "subscribe_topics": ["onex.cmd.omnimarket.sample-start.v1"],
            "publish_topics": [topic],
        },
    }
    if stub:
        payload["node_not_implemented"] = True
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.mark.unit
def test_gap_detect_clean_fixture(tmp_path: Path) -> None:
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="onex.evt.omnimarket.sample-completed.v1",
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.CLEAN
    assert result.contracts_checked == 1
    assert result.findings == []


@pytest.mark.unit
def test_gap_detect_reports_stub_and_bad_topic(tmp_path: Path) -> None:
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="not-a-topic",
        stub=True,
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.FINDINGS
    assert {finding.rule_name for finding in result.findings} == {
        "node_not_implemented",
        "topic_name_mismatch",
    }


@pytest.mark.unit
def test_gap_detect_deduplicates_repeated_contract_topics(tmp_path: Path) -> None:
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="not-a-topic",
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert [finding.rule_name for finding in result.findings] == ["topic_name_mismatch"]


@pytest.mark.unit
def test_gap_detect_skips_malformed_contract_and_continues(tmp_path: Path) -> None:
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="onex.evt.omnimarket.sample-completed.v1",
    )
    malformed = repo / "src/omnimarket/nodes/node_bad/contract.yaml"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("name: [unterminated\n", encoding="utf-8")

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.CLEAN
    assert result.contracts_checked == 1
    assert any(probe.probe == "contract_parse" for probe in result.skipped_probes)


@pytest.mark.unit
def test_gap_detect_defaults_to_current_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "omnimarket"
    sibling = tmp_path / "sibling_repo"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="onex.evt.omnimarket.sample-completed.v1",
    )
    _write_contract(
        sibling / "src/sibling/nodes/node_sample/contract.yaml",
        topic="not-a-topic",
    )
    monkeypatch.setattr(handler_gap_compute, "_REPO_ROOT", repo)

    result = HandlerGapCompute().handle(ModelGapComputeRequest(dry_run=True))

    assert result.repos_in_scope == ["omnimarket"]
    assert result.status == EnumGapStatus.CLEAN


@pytest.mark.unit
def test_gap_fix_requires_report() -> None:
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(subcommand="fix", dry_run=True)
    )

    assert result.status == EnumGapStatus.BLOCKED
    assert result.skipped_probes[0].reason == "REPORT_REQUIRED"


@pytest.mark.unit
def test_gap_cli_outputs_json(tmp_path: Path) -> None:
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic="onex.evt.omnimarket.sample-completed.v1",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_gap_compute",
            "detect",
            "--repo-root",
            str(repo),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    result = ModelGapComputeResult.model_validate(json.loads(completed.stdout))
    assert result.status == EnumGapStatus.CLEAN
    assert result.dry_run is True
