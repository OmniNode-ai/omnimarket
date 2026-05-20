# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract and handler tests for persona lifecycle orchestration."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_persona_lifecycle_orchestrator import (
    HandlerPersonaRebuild,
    ModelPersonaLifecycleRequest,
    ModelPersonaLifecycleResponse,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_persona_lifecycle_orchestrator"
    / "contract.yaml"
)


class _CandidateProvider:
    def __init__(self, user_ids: list[str]) -> None:
        self.user_ids = user_ids
        self.limits: list[int] = []

    async def list_persona_rebuild_candidates(self, limit: int) -> list[str]:
        self.limits.append(limit)
        return self.user_ids


class _RebuildPort:
    def __init__(self, skipped: set[str] | None = None) -> None:
        self.skipped = skipped or set()
        self.calls: list[tuple[str, UUID | None]] = []

    async def rebuild_persona(
        self,
        user_id: str,
        correlation_id: UUID | None = None,
    ) -> bool:
        self.calls.append((user_id, correlation_id))
        return user_id not in self.skipped


def _load_contract() -> dict[str, object]:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(data, dict)
    return data


def test_contract_routes_resolve_to_real_handler_methods() -> None:
    contract = _load_contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)

    handlers = routing["handlers"]
    assert isinstance(handlers, list)
    assert handlers

    for entry in handlers:
        assert isinstance(entry, dict)
        module_name = entry["handler_module"]
        handler_key = entry["handler_key"]
        assert isinstance(module_name, str)
        assert isinstance(handler_key, str)

        class_name, method_name = handler_key.split(".", maxsplit=1)
        module = importlib.import_module(module_name)
        handler_class = getattr(module, class_name)
        handler = handler_class()

        assert handler_class is HandlerPersonaRebuild
        assert callable(getattr(handler, method_name))


def test_contract_declares_memory_runtime_ownership() -> None:
    contract = _load_contract()

    assert contract["node_type"] == "orchestrator"
    assert contract["runtime_profiles"] == ["memory"]

    metadata = contract["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["runtime_owner"] == "memory"


def test_model_exports_resolve_from_node_package() -> None:
    request = ModelPersonaLifecycleRequest(operation="on_demand", user_id="user_1")
    response = ModelPersonaLifecycleResponse(status="success", users_processed=1)

    assert request.operation == "on_demand"
    assert request.user_id == "user_1"
    assert response.status == "success"
    assert response.users_processed == 1


def test_on_demand_rebuilds_single_user() -> None:
    correlation_id = uuid4()
    rebuild_port = _RebuildPort()
    handler = HandlerPersonaRebuild(rebuild_port=rebuild_port)

    response = asyncio.run(
        handler.on_demand(
            correlation_id,
            ModelPersonaLifecycleRequest(operation="on_demand", user_id="user_1"),
        )
    )

    assert response == ModelPersonaLifecycleResponse(
        status="success",
        users_processed=1,
        personas_created=1,
        users_skipped=0,
    )
    assert rebuild_port.calls == [("user_1", correlation_id)]


def test_on_demand_without_user_id_returns_contract_error() -> None:
    handler = HandlerPersonaRebuild(rebuild_port=_RebuildPort())

    response = asyncio.run(
        handler.on_demand(
            uuid4(),
            ModelPersonaLifecycleRequest(operation="on_demand"),
        )
    )

    assert response.status == "error"
    assert response.error_message == "on_demand persona rebuild requires user_id"


def test_on_tick_rebuilds_candidates_with_contract_batch_cap() -> None:
    user_ids = [f"user_{index}" for index in range(105)]
    candidate_provider = _CandidateProvider(user_ids)
    rebuild_port = _RebuildPort(skipped={"user_3"})
    handler = HandlerPersonaRebuild(
        candidate_provider=candidate_provider,
        rebuild_port=rebuild_port,
        batch_size=250,
    )

    response = asyncio.run(
        handler.on_tick(uuid4(), ModelPersonaLifecycleRequest(operation="on_tick"))
    )

    assert candidate_provider.limits == [100]
    assert response.status == "success"
    assert response.users_processed == 100
    assert response.personas_created == 99
    assert response.users_skipped == 1
    assert len(rebuild_port.calls) == 100
    assert rebuild_port.calls[-1][0] == "user_99"


def test_unconfigured_handler_returns_explicit_error() -> None:
    handler = HandlerPersonaRebuild()

    response = asyncio.run(
        handler.on_tick(uuid4(), ModelPersonaLifecycleRequest(operation="on_tick"))
    )

    assert response.status == "error"
    assert (
        response.error_message == "persona rebuild candidate provider is not configured"
    )
