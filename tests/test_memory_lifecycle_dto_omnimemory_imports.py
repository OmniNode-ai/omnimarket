# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility tests for memory lifecycle DTO imports."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

DTO_IMPORTS = {
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelArchiveMemoryCommand": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelArchiveMemoryCommand",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelArchiveRecord": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelArchiveRecord",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelCircuitBreakerConfigInfo": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelCircuitBreakerConfigInfo",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveHealth": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveHealth",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveMetadata": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveMetadata",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveResult": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryArchiveResult",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryRow": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_archive.ModelMemoryRow",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelExpireMemoryCommand": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelExpireMemoryCommand",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryCurrentState": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryCurrentState",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireHealth": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireHealth",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireMetadata": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireMetadata",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireResult": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_expire.ModelMemoryExpireResult",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryArchiveInitiated": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryArchiveInitiated",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryExpiredEvent": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryExpiredEvent",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryLifecycleProjection": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryLifecycleProjection",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickHealth": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickHealth",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickMetadata": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickMetadata",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickResult": "omnimemory.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.ModelMemoryTickResult",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition.ModelTransitionValidationResult": "omnimemory.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition.ModelTransitionValidationResult",
    "omnimarket.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition.VALID_TRANSITIONS": "omnimemory.nodes.node_memory_lifecycle_orchestrator.validators.validator_lifecycle_transition.VALID_TRANSITIONS",
}


def _resolve(dotted_ref: str) -> Any:
    module_name, _, attr = dotted_ref.rpartition(".")
    return getattr(import_module(module_name), attr)


def _memory_lifecycle_contract() -> dict[str, Any]:
    contract_path = Path(
        resources.files("omnimarket.nodes")
        / "node_memory_lifecycle_orchestrator"
        / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
    assert isinstance(contract, dict)
    return contract


def _collect_contract_model_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"input_model", "output_model", "schema_ref"}:
                if isinstance(item, str) and "." in item:
                    refs.append(item)
                continue
            refs.extend(_collect_contract_model_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_contract_model_refs(item))
    return refs


@pytest.mark.unit
@pytest.mark.parametrize(("market_ref", "memory_ref"), DTO_IMPORTS.items())
def test_memory_lifecycle_dto_market_paths_reexport_omnimemory_objects(
    market_ref: str, memory_ref: str
) -> None:
    """Existing omnimarket lifecycle paths resolve to canonical omnimemory DTOs."""
    market_model = _resolve(market_ref)
    memory_model = _resolve(memory_ref)

    assert market_model is memory_model
    if hasattr(market_model, "__module__"):
        assert market_model.__module__.startswith("omnimemory.")


@pytest.mark.unit
def test_memory_lifecycle_package_exports_reexport_omnimemory_classes() -> None:
    """Package-level compatibility exports return canonical omnimemory DTO classes."""
    from omnimarket.nodes.node_memory_lifecycle_orchestrator import (
        ModelArchiveMemoryCommand,
        ModelArchiveRecord,
        ModelExpireMemoryCommand,
        ModelMemoryArchiveResult,
        ModelMemoryCurrentState,
        ModelMemoryExpireResult,
        ModelMemoryTickResult,
    )

    exported_models = [
        ModelArchiveMemoryCommand,
        ModelArchiveRecord,
        ModelExpireMemoryCommand,
        ModelMemoryArchiveResult,
        ModelMemoryCurrentState,
        ModelMemoryExpireResult,
        ModelMemoryTickResult,
    ]

    assert all(model.__module__.startswith("omnimemory.") for model in exported_models)


@pytest.mark.unit
def test_memory_lifecycle_contract_schema_refs_resolve_to_canonical_classes() -> None:
    """Contract schema refs can keep omnimarket paths through compatibility aliases."""
    contract = _memory_lifecycle_contract()
    event_bus = contract["event_bus"]

    schema_refs = [
        metadata["schema_ref"]
        for metadata_key in ("subscribe_topic_metadata", "publish_topic_metadata")
        for metadata in event_bus.get(metadata_key, {}).values()
        if metadata.get("schema_ref", "").startswith(
            "omnimarket.nodes.node_memory_lifecycle_orchestrator"
        )
    ]

    assert schema_refs
    assert all(
        _resolve(schema_ref).__module__.startswith("omnimemory.")
        for schema_ref in schema_refs
    )


@pytest.mark.unit
def test_memory_lifecycle_contract_model_refs_resolve() -> None:
    """Every active model reference in the contract resolves to importable code."""
    contract = _memory_lifecycle_contract()

    model_refs = _collect_contract_model_refs(contract)

    assert model_refs
    for model_ref in model_refs:
        model = _resolve(model_ref)
        assert model is not None, model_ref
        if model_ref.startswith(
            "omnimarket.nodes.node_memory_lifecycle_orchestrator.models"
        ):
            assert issubclass(model, BaseModel)


@pytest.mark.unit
def test_memory_lifecycle_contract_declares_orchestrator_runtime_ownership() -> None:
    """Memory lifecycle is an effectful orchestrator owned by the effects runtime."""
    contract = _memory_lifecycle_contract()
    descriptor = contract["descriptor"]

    assert contract["node_type"] == "orchestrator"
    assert descriptor["node_archetype"] == "orchestrator"
    assert descriptor["purity"] == "effectful"
    assert descriptor["runtime_profiles"] == ["effects"]


@pytest.mark.unit
def test_memory_lifecycle_orchestrator_envelope_models_validate_operations() -> None:
    """Contract I/O models require operation-specific payloads."""
    from uuid import uuid4

    from omnimarket.nodes.node_memory_lifecycle_orchestrator.models import (
        EnumLifecycleOrchestratorOperation,
        EnumLifecycleOrchestratorStatus,
        ModelLifecycleOrchestratorInput,
        ModelLifecycleOrchestratorOutput,
    )

    command = _resolve(
        "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers."
        "handler_memory_expire.ModelExpireMemoryCommand"
    )(memory_id=uuid4(), expected_revision=0)
    input_model = ModelLifecycleOrchestratorInput(
        operation=EnumLifecycleOrchestratorOperation.EXPIRE,
        correlation_id=uuid4(),
        expire_command=command,
    )
    output_model = ModelLifecycleOrchestratorOutput(
        status=EnumLifecycleOrchestratorStatus.COMPLETED,
        operation=EnumLifecycleOrchestratorOperation.EXPIRE,
        correlation_id=input_model.correlation_id,
        processed_memory_count=1,
    )

    assert input_model.expire_command is command
    assert output_model.processed_memory_count == 1
    with pytest.raises(
        ValidationError, match="archive operation requires archive_command"
    ):
        ModelLifecycleOrchestratorInput(
            operation=EnumLifecycleOrchestratorOperation.ARCHIVE,
            correlation_id=uuid4(),
        )
    with pytest.raises(ValidationError, match="failed status requires error_message"):
        ModelLifecycleOrchestratorOutput(
            status=EnumLifecycleOrchestratorStatus.FAILED,
            operation=EnumLifecycleOrchestratorOperation.EXPIRE,
            correlation_id=uuid4(),
        )
