# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17099: local served model ids belong to the lane overlay."""

from __future__ import annotations

import importlib.resources
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def packaged_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Bind the packaged base explicitly, excluding any machine-local overlay."""
    resource = importlib.resources.files("omnimarket").joinpath(
        "configs/bifrost_delegation.yaml"
    )
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
    tiers = tmp_path / "routing_tiers.yaml"
    tiers.write_text(
        importlib.resources.files("omnimarket")
        .joinpath("configs/routing_tiers.yaml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield contract
    finally:
        routing._config = None
        routing._load_bifrost_endpoints.cache_clear()


def test_every_packaged_local_backend_defers_its_model_to_the_overlay(
    packaged_base: Path,
) -> None:
    contract = yaml.safe_load(packaged_base.read_text(encoding="utf-8"))
    local = [backend for backend in contract["backends"] if backend["tier"] == "local"]
    assert {backend["backend_id"] for backend in local} == {
        "local-coder",
        "local-heavy-reasoning",
        "local-embedding",
    }
    assert all(backend["model_name"] is None for backend in local)


def test_base_without_overlay_loads_but_local_rungs_are_unroutable(
    packaged_base: Path,
) -> None:
    config = load_bifrost_delegation_config(config_path=packaged_base)
    local = [backend for backend in config.backends if backend.tier == "local"]
    assert local
    assert all(
        backend.endpoint_url is None and backend.model_name is None for backend in local
    )
    endpoints = routing._load_bifrost_endpoints()
    assert endpoints  # Cloud declarations still load.
    assert all(backend.backend_id not in endpoints for backend in local)
    assert routing._get_config().tiers


def test_local_pick_posts_the_overlay_served_id_instead_of_the_tier_key(
    packaged_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": "local-coder",
                        "endpoint_url": "http://developer.invalid:8000/v1/chat/completions",
                        "model_name": "dev-chosen-model-x",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    endpoints = routing._load_bifrost_endpoints()
    assert "local-coder" in endpoints
    assert not {"local-heavy-reasoning", "local-embedding"} & endpoints.keys()
    decision = routing.delta(
        ModelDelegationRequest(
            prompt="Write a small function that adds two integers.",
            task_type="code_generation",
            correlation_id=uuid4(),
            emitted_at=datetime.now(UTC),
        )
    )
    assert decision.tier_name == "local"
    assert decision.selected_backend_ref == "local-coder"
    assert decision.selected_model == "dev-chosen-model-x"
    assert decision.endpoint_url == "http://developer.invalid:8000/v1/chat/completions"
    assert all(
        model.id != "dev-chosen-model-x"
        for tier in routing._get_config().tiers
        for model in tier.models
    )
