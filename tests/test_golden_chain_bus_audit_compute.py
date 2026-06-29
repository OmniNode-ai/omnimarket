from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.events.topics import OMNICLAUDE_EVT_TOPIC_PREFIX
from omnimarket.nodes.node_bus_audit_compute.handlers.handler_bus_audit_compute import (
    HandlerBusAuditCompute,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_result import (
    EnumBusAuditFindingType,
    EnumBusAuditStatus,
    ModelBusAuditComputeResult,
)


def _write_registry(path: Path, *, topic: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "events": {
                    "sample.completed": {
                        "fan_out": [
                            {
                                "topic": topic,
                                "description": "Sample completion.",
                            }
                        ],
                        "partition_key_field": "run_id",
                        "required_fields": ["run_id"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _write_contract(path: Path, *, topic: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "name": "node_sample",
                "node_type": "compute",
                "terminal_event": topic,
                "event_bus": {
                    "subscribe_topics": ["onex.cmd.omnimarket.sample-start.v1"],
                    "publish_topics": [topic],
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_bus_audit_clean_fixture(tmp_path: Path) -> None:
    registry = tmp_path / "topics.yaml"
    contract = tmp_path / "nodes/node_sample/contract.yaml"
    topic = "onex.evt.omnimarket.sample-completed.v1"
    _write_registry(registry, topic=topic)
    _write_contract(contract, topic=topic)

    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(tmp_path / "nodes")],
            dry_run=True,
        )
    )

    assert result.status == EnumBusAuditStatus.CLEAN
    assert result.topics_registered == 1
    assert result.topics_declared == 2
    assert result.contracts_checked == 1
    assert result.findings == []


@pytest.mark.unit
def test_bus_audit_reports_invalid_registry_topic(tmp_path: Path) -> None:
    registry = tmp_path / "topics.yaml"
    contract = tmp_path / "nodes/node_sample/contract.yaml"
    _write_registry(registry, topic="not-a-topic")
    _write_contract(contract, topic="onex.evt.omnimarket.sample-completed.v1")

    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(tmp_path / "nodes")],
            dry_run=True,
        )
    )

    assert result.status == EnumBusAuditStatus.ERROR
    assert any(
        finding.finding_type == EnumBusAuditFindingType.INVALID_TOPIC_NAME
        for finding in result.findings
    )


@pytest.mark.unit
def test_bus_audit_cli_outputs_result_model(tmp_path: Path) -> None:
    registry = tmp_path / "topics.yaml"
    contract = tmp_path / "nodes/node_sample/contract.yaml"
    topic = "onex.evt.omnimarket.sample-completed.v1"
    _write_registry(registry, topic=topic)
    _write_contract(contract, topic=topic)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_bus_audit_compute",
            "--registry-path",
            str(registry),
            "--contract-root",
            str(tmp_path / "nodes"),
            "--dry-run",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    result = ModelBusAuditComputeResult.model_validate(json.loads(completed.stdout))
    assert result.status == EnumBusAuditStatus.CLEAN
    assert result.dry_run is True


@pytest.mark.unit
def test_omniclaude_evt_topic_prefix_constant_value() -> None:
    """OMNICLAUDE_EVT_TOPIC_PREFIX must be the canonical prefix for omniclaude event topics.

    This test pins the constant value so that any rename surfaces as a
    compile-time failure rather than a silent audit regression.
    """
    assert OMNICLAUDE_EVT_TOPIC_PREFIX == "onex.evt.omniclaude."


@pytest.mark.unit
def test_bus_audit_detects_unregistered_omniclaude_topic(tmp_path: Path) -> None:
    """CONTRACT_TOPIC_UNREGISTERED is raised for an omniclaude topic absent from the registry.

    The handler must use OMNICLAUDE_EVT_TOPIC_PREFIX (not a literal) to match
    omniclaude namespace topics. Verifies the detection logic remains correct
    after the constant-extraction refactor.
    """
    registry = tmp_path / "topics.yaml"
    contract = tmp_path / "nodes/node_sample/contract.yaml"

    registered_topic = "onex.evt.omniclaude.session-started.v1"
    unregistered_topic = "onex.evt.omniclaude.session-ended.v1"

    # Registry contains only one of the two omniclaude topics declared in the contract.
    registry.write_text(
        yaml.safe_dump(
            {
                "events": {
                    "session.started": {
                        "fan_out": [
                            {
                                "topic": registered_topic,
                                "description": "Session started.",
                            }
                        ],
                        "partition_key_field": "session_id",
                        "required_fields": ["session_id"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        yaml.safe_dump(
            {
                "name": "node_sample",
                "node_type": "compute",
                "terminal_event": registered_topic,
                "event_bus": {
                    "subscribe_topics": ["onex.cmd.omnimarket.sample-start.v1"],
                    "publish_topics": [registered_topic, unregistered_topic],
                },
            }
        ),
        encoding="utf-8",
    )

    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(tmp_path / "nodes")],
            dry_run=True,
        )
    )

    unregistered_findings = [
        f
        for f in result.findings
        if f.finding_type == EnumBusAuditFindingType.CONTRACT_TOPIC_UNREGISTERED
    ]
    assert len(unregistered_findings) == 1, (
        f"Expected exactly one CONTRACT_TOPIC_UNREGISTERED finding; got {result.findings}"
    )
    assert unregistered_findings[0].subject == unregistered_topic
