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


def _write_contract_without_event_bus(
    path: Path, *, node_type: str | None, name: str = "node_sample"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"name": name}
    if node_type is not None:
        payload["node_type"] = node_type
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
@pytest.mark.parametrize("node_type", ["compute", "COMPUTE_GENERIC", "transformer"])
def test_missing_event_bus_skipped_for_pure_compute(
    tmp_path: Path, node_type: str
) -> None:
    """Pure-compute archetypes missing event_bus are SKIP, not a WARNING finding."""
    repo = tmp_path / "omnimarket"
    _write_contract_without_event_bus(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        node_type=node_type,
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.CLEAN
    assert all(
        finding.rule_name != "missing_event_bus_contract" for finding in result.findings
    )
    assert any(
        probe.probe == "missing_event_bus_contract" for probe in result.skipped_probes
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_type", ["orchestrator", "reducer", "effect"])
def test_missing_event_bus_warns_for_bus_archetypes(
    tmp_path: Path, node_type: str
) -> None:
    """Orchestrator/reducer/effect missing event_bus keep the WARNING finding."""
    repo = tmp_path / "omnimarket"
    _write_contract_without_event_bus(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        node_type=node_type,
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.FINDINGS
    assert [finding.rule_name for finding in result.findings] == [
        "missing_event_bus_contract"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("node_type", [None, "totally_unknown_archetype"])
def test_missing_event_bus_warns_for_unknown_node_type(
    tmp_path: Path, node_type: str | None
) -> None:
    """Unknown/absent node_type fails loud: keep the WARNING, never silently skip."""
    repo = tmp_path / "omnimarket"
    _write_contract_without_event_bus(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        node_type=node_type,
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.FINDINGS
    assert [finding.rule_name for finding in result.findings] == [
        "missing_event_bus_contract"
    ]


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
def test_gap_detect_defaults_to_omni_home_repo_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-arg detect resolves the canonical ``$OMNI_HOME`` repo set (OMN-13534).

    The prior behaviour defaulted to ``_REPO_ROOT`` (``parents[5]``), which on
    the deployed ``onex skill gap`` path resolved a ``python3.12`` version-token
    root and false-cleaned. The corrected contract enumerates the canonical
    default repos under ``$OMNI_HOME`` so a no-arg dispatch scans the real repo
    universe (OMN-13538).
    """
    from omnimarket.nodes.sweep_scope import DEFAULT_REPOS

    omni_home = tmp_path / "omni_home"
    for repo in DEFAULT_REPOS:
        (omni_home / repo).mkdir(parents=True)
    # Give one canonical repo a clean sample contract so detect has real input.
    _write_contract(
        omni_home / DEFAULT_REPOS[0] / "src/nodes/node_sample/contract.yaml",
        topic="onex.evt.omnimarket.sample-completed.v1",
    )
    monkeypatch.setenv("OMNI_HOME", str(omni_home))

    result = HandlerGapCompute().handle(ModelGapComputeRequest(dry_run=True))

    assert set(result.repos_in_scope) == set(DEFAULT_REPOS)
    assert "python3.12" not in result.repos_in_scope
    assert result.status == EnumGapStatus.CLEAN


@pytest.mark.unit
def test_gap_fix_requires_report() -> None:
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(subcommand="fix", dry_run=True)
    )

    assert result.status == EnumGapStatus.BLOCKED
    assert result.skipped_probes[0].reason == "REPORT_REQUIRED"


@pytest.mark.unit
def test_gap_contract_operation_match_handler_declares_operation() -> None:
    """Every operation_match handler must declare an ``operation``.

    The runtime's receipt-mode dispatch (``onex skill gap ...``) validates
    handler_routing and fails with ``handlers[N].operation is missing`` when an
    ``operation_match`` handler omits the field. The handler and the ``__main__``
    CLI path do not exercise this validation, so it was the gap that let the
    bus-dispatch path break while the node "ran fine directly".
    """
    contract_path = (
        Path(handler_gap_compute.__file__).resolve().parents[1] / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    for index, entry in enumerate(routing["handlers"]):
        operation = entry.get("operation")
        assert operation, (
            f"handler_routing.handlers[{index}] must declare a non-empty "
            f"'operation' for operation_match dispatch; got {operation!r}"
        )


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
