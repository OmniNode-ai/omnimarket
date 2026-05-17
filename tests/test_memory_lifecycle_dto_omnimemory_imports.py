# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility tests for memory lifecycle DTO imports."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    contract_path = Path(
        resources.files("omnimarket.nodes")
        / "node_memory_lifecycle_orchestrator"
        / "contract.yaml"  # type: ignore[arg-type]
    )
    contract = yaml.safe_load(contract_path.read_text())
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
