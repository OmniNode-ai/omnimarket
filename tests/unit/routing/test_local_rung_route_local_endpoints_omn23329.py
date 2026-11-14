# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23329: the routing overlay's ``local_endpoints`` can declare the keyed small local rung.

The house deployment declares its local GPU backends in the delegation routing
overlay (``local_endpoints``, OMN-23871). The small local model server sits behind
an API key, so its row names a ``secret_ref`` and a ``lead`` placement. These tests
route over the packaged ladder, the packaged task class contracts and a routing
overlay carrying that row, and pin:

* the row loads with its ``secret_ref`` and places the backend first in its tier for
  the classes it names, with the overlay's endpoint and model;
* a row naming ``api_key_env`` or ``api_key_ref`` still refuses (``secret_ref`` is the
  one credential surface);
* an unresolved secret falls through to the tier's own rung;
* with the row removed nothing is led (the failing control).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.routing.delegation_backend_resolution import (
    load_bifrost_backends,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_KEY = "DELEGATION_ROUTING_OVERLAY_PATH"
_LEAD = "local-small"
_LEAD_REF = "llm.local_small.api_key"
_LEAD_URL = "http://198.51.100.30:8110/v1/chat/completions"
_LEAD_MODEL = "small-local-model-q4"
_TIER_RUNG = "local-heavy-reasoning"
_SERVED = ("summarization", "evidence_comment", "tail_summary")
_GPU = "http://198.51.100.10:8000/v1/chat/completions"
_GPU_MODEL = "fixture-rung-model"


def _lead_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "backend_id": _LEAD,
        "provider": "local",
        "tier": "local",
        "endpoint_url": _LEAD_URL,
        "model_name": _LEAD_MODEL,
        "timeout_ms": 60000,
        "max_tokens": 512,
        "secret_ref": _LEAD_REF,
        "capabilities": ["summarization"],
        "placement": {
            "tier": "local",
            "mode": "lead",
            "max_context_tokens": 12000,
            "use_for": list(_SERVED),
        },
    }
    row.update(overrides)
    return row


def _rows(*, with_lead: bool = True, **lead_overrides: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "backend_id": backend_id,
            "provider": "local",
            "tier": "local",
            "endpoint_url": _GPU,
            "model_name": _GPU_MODEL,
        }
        for backend_id in ("local-coder", _TIER_RUNG)
    ]
    if with_lead:
        rows.append(_lead_row(**lead_overrides))
    return rows


def _reset() -> None:
    routing._config = None
    routing._config_binding = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rows: list[dict[str, Any]],
    *,
    credential: bool = True,
) -> None:
    overlay = tmp_path / "routing_overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": "delegation_routing_overlay.v1",
                "local_endpoints": {"backends": rows},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_KEY, str(overlay))
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no_bifrost.yaml"))
    # A provider ref resolves through the machine's secret store, which a unit test
    # does not seed: every ref is taken as resolved, except the lead's when
    # ``credential`` is False.
    monkeypatch.setattr(
        routing,
        "_backend_secret_available",
        lambda backend: credential or backend.api_key_ref != _LEAD_REF,
    )
    _reset()


@pytest.fixture
def _bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[None, None, None]:
    _bind(monkeypatch, tmp_path, _rows())
    yield
    _reset()


def _request(task_type: str) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize: the build passed.",
        task_type=task_type,
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )


@pytest.mark.usefixtures("_bound")
@pytest.mark.parametrize("task_type", _SERVED)
def test_local_rung_route_local_endpoints_leads_for_every_served_class(
    task_type: str,
) -> None:
    decision = routing.delta(_request(task_type))
    assert decision.selected_backend_ref == _LEAD
    assert decision.tier_name == "local"
    assert decision.endpoint_url == _LEAD_URL
    assert decision.selected_model == _LEAD_MODEL


@pytest.mark.usefixtures("_bound")
def test_local_rung_route_local_endpoints_names_the_lead() -> None:
    assert routing.lead_backend_refs() == frozenset({_LEAD})


def test_local_rung_route_local_endpoints_row_carries_its_secret_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, _rows())
    try:
        row = next(
            b
            for b in load_bifrost_backends(overlay_path=tmp_path / "no_bifrost.yaml")
            if b["backend_id"] == _LEAD
        )
        assert row["secret_ref"] == _LEAD_REF
        assert row["endpoint_url"] == _LEAD_URL
        assert row["model_name"] == _LEAD_MODEL
    finally:
        _reset()


def test_local_rung_route_local_endpoints_unresolved_secret_is_not_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path, _rows(), credential=False)
    try:
        decision = routing.delta(_request("summarization"))
        assert decision.selected_backend_ref == _TIER_RUNG
    finally:
        _reset()


def test_local_rung_route_local_endpoints_without_the_row_leads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failing control: the same overlay with the lead row removed."""
    _bind(monkeypatch, tmp_path, _rows(with_lead=False))
    try:
        assert routing.lead_backend_refs() == frozenset()
        for task_type in _SERVED:
            decision = routing.delta(_request(task_type))
            assert decision.selected_backend_ref == _TIER_RUNG
            assert decision.selected_model != _LEAD_MODEL
    finally:
        _reset()


@pytest.mark.parametrize(
    "credential_key", ["api_key_env", "api_key_ref"], ids=["env", "ref"]
)
def test_local_rung_route_local_endpoints_other_credential_surfaces_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential_key: str
) -> None:
    _bind(
        monkeypatch,
        tmp_path,
        _rows(**{"secret_ref": None, credential_key: "LLM_LOCAL_SMALL_KEY"}),
    )
    try:
        with pytest.raises(ProtocolConfigurationError) as exc:
            load_bifrost_backends(overlay_path=tmp_path / "no_bifrost.yaml")
        assert _LEAD in str(exc.value)
        assert "secret_ref" in str(exc.value)
        assert "LLM_LOCAL_SMALL_KEY" not in str(exc.value)
    finally:
        _reset()
