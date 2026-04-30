from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
