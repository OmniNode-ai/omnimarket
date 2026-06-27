# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_bus_audit_compute (OMN-13676).

COMPUTE node, Variant A. The handler's only I/O boundary is reading the event
registry YAML and node ``contract.yaml`` files from paths supplied on the
request. We inject that boundary the clean way: write synthetic registry and
contract fixtures under ``tmp_path`` and point the request at them — no
monkeypatching of open/yaml/subprocess. Each case asserts typed result fields
(status, finding types, counts); the negative-control cases must surface a
concrete finding of the expected type.
"""

from __future__ import annotations

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
    EnumBusAuditSeverity,
    EnumBusAuditStatus,
)


def _write_registry(tmp_path: Path, events: dict[str, object]) -> Path:
    registry = tmp_path / "topics.yaml"
    registry.write_text(yaml.safe_dump({"events": events}), encoding="utf-8")
    return registry


def _write_contract(
    tmp_path: Path, *, name: str, event_bus: dict[str, object] | None
) -> Path:
    """Write a single synthetic node contract under its own node dir."""
    node_dir = tmp_path / "contracts" / name
    node_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"name": name}
    if event_bus is not None:
        body["event_bus"] = event_bus
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return node_dir


_CLEAN_EVENT = {
    "clean_event": {
        "partition_key_field": "correlation_id",
        "required_fields": ["correlation_id"],
        "fan_out": [{"topic": "onex.evt.omnimarket.clean-event.v1"}],
    }
}


@pytest.mark.integration
def test_bus_audit_clean(tmp_path: Path) -> None:
    """(a) Valid registry + valid contract → CLEAN, zero findings."""
    registry = _write_registry(tmp_path, _CLEAN_EVENT)
    contracts = _write_contract(
        tmp_path,
        name="node_clean",
        event_bus={"publish_topics": ["onex.evt.omnimarket.clean-event.v1"]},
    )
    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(contracts.parent)],
        )
    )
    assert result.status == EnumBusAuditStatus.CLEAN
    assert result.findings == []
    assert result.topics_registered == 1
    assert result.contracts_checked == 1
    assert result.topics_declared == 1


@pytest.mark.integration
def test_bus_audit_registry_not_found(tmp_path: Path) -> None:
    """(b) Negative control: missing registry → ERROR REGISTRY_NOT_FOUND."""
    contracts = _write_contract(
        tmp_path,
        name="node_clean",
        event_bus={"publish_topics": ["onex.evt.omnimarket.clean-event.v1"]},
    )
    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(tmp_path / "does_not_exist.yaml"),
            contract_roots=[str(contracts.parent)],
        )
    )
    assert result.status == EnumBusAuditStatus.ERROR
    types = {f.finding_type for f in result.findings}
    assert EnumBusAuditFindingType.REGISTRY_NOT_FOUND in types
    assert result.error_count >= 1


@pytest.mark.integration
def test_bus_audit_missing_fan_out(tmp_path: Path) -> None:
    """(c) Negative control: registry event with no fan_out → ERROR MISSING_FAN_OUT."""
    registry = _write_registry(
        tmp_path,
        {"orphan_event": {"partition_key_field": "cid", "required_fields": ["cid"]}},
    )
    contracts = _write_contract(tmp_path, name="node_x", event_bus=None)
    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(contracts.parent)],
        )
    )
    assert result.status == EnumBusAuditStatus.ERROR
    types = {f.finding_type for f in result.findings}
    assert EnumBusAuditFindingType.MISSING_FAN_OUT in types
    # The contract without event_bus must also be flagged.
    assert EnumBusAuditFindingType.CONTRACT_EVENT_BUS_MISSING in types


@pytest.mark.integration
def test_bus_audit_duplicate_topic(tmp_path: Path) -> None:
    """(d) Negative control: same topic in two events → WARNING DUPLICATE_TOPIC."""
    registry = _write_registry(
        tmp_path,
        {
            "event_a": {
                "partition_key_field": "cid",
                "required_fields": ["cid"],
                "fan_out": [{"topic": "onex.evt.omnimarket.dup.v1"}],
            },
            "event_b": {
                "partition_key_field": "cid",
                "required_fields": ["cid"],
                "fan_out": [{"topic": "onex.evt.omnimarket.dup.v1"}],
            },
        },
    )
    contracts = _write_contract(
        tmp_path,
        name="node_clean",
        event_bus={"publish_topics": ["onex.evt.omnimarket.dup.v1"]},
    )
    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(contracts.parent)],
        )
    )
    assert result.status == EnumBusAuditStatus.FINDINGS
    types = {f.finding_type for f in result.findings}
    assert EnumBusAuditFindingType.DUPLICATE_TOPIC in types
    assert result.warning_count >= 1


@pytest.mark.integration
def test_bus_audit_failures_only_filter(tmp_path: Path) -> None:
    """(e) failures_only=True drops WARNINGs, keeps only ERROR findings."""
    registry = _write_registry(
        tmp_path,
        {
            # ERROR: no fan_out.
            "orphan_event": {"partition_key_field": "cid", "required_fields": ["cid"]},
            # WARNING source: missing partition key + required fields.
            "weak_event": {"fan_out": [{"topic": "onex.evt.omnimarket.weak.v1"}]},
        },
    )
    contracts = _write_contract(
        tmp_path,
        name="node_clean",
        event_bus={"publish_topics": ["onex.evt.omnimarket.weak.v1"]},
    )
    result = HandlerBusAuditCompute().handle(
        ModelBusAuditComputeRequest(
            registry_path=str(registry),
            contract_roots=[str(contracts.parent)],
            failures_only=True,
        )
    )
    assert result.status == EnumBusAuditStatus.ERROR
    assert result.findings, "expected at least one ERROR finding"
    assert all(f.severity == EnumBusAuditSeverity.ERROR for f in result.findings), (
        "failures_only must drop non-ERROR findings"
    )
    assert result.warning_count == 0
