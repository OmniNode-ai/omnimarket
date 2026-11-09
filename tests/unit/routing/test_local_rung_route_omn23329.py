# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23329: the small local model is the first rung for the classes it serves.

The delegation chain starts local and escalates on measured results. A host's
routing overlay (``bifrost_overrides.yaml``) can ADD a backend with a ``lead``
placement: the backend becomes the first rung of its tier for the classes it
names, with its endpoint and served model in the overlay and nowhere in a
package file. These tests route over the packaged ladder, the packaged task
class contracts and a fixture overlay, and pin:

* every served class is routed to the lead rung, with the overlay's endpoint
  and model;
* a class the overlay does not name, a prompt over the rung's window and an
  unresolved credential all fall through to the tier's own rung;
* a quality-gate miss on the lead rung moves the tier on to its own rung, and
  that rung's retry budget is not spent on the lead;
* with the overlay entry removed nothing is led (the failing control).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation"
    / "omn23329"
    / "bifrost_overrides.local_rung.yaml"
)
_LEAD = "local-small"
_LEAD_REF = "llm.local_small.api_key"
_LEAD_URL = "http://198.51.100.30:8110/v1/chat/completions"
_LEAD_MODEL = "small-local-model-q4"
_TIER_RUNG = "local-heavy-reasoning"
_SERVED = ("summarization", "evidence_comment", "tail_summary")
_NOT_SERVED = ("document", "research", "planning")


def _reset() -> None:
    routing._config = None
    routing._config_binding = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._config_lead_refs = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _bind(
    monkeypatch: pytest.MonkeyPatch, overlay: Path, *, credential: bool = True
) -> None:
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    # A provider ref (llm.<name>.api_key) resolves through the machine's secret
    # store, which a unit test does not seed. Every backend's ref is taken as
    # resolved, except the lead's when ``credential`` is False.
    monkeypatch.setattr(
        routing,
        "_backend_secret_available",
        lambda backend: credential or backend.api_key_ref != _LEAD_REF,
    )
    _reset()


@pytest.fixture
def _overlay(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    _bind(monkeypatch, _FIXTURE)
    yield
    _reset()


def _without_entry(tmp_path: Path) -> Path:
    document = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    document["backends"] = [
        entry for entry in document["backends"] if entry["backend_id"] != _LEAD
    ]
    stripped = tmp_path / "bifrost_overrides.no_local_rung.yaml"
    stripped.write_text(yaml.safe_dump(document), encoding="utf-8")
    return stripped


def _request(
    task_type: str, prompt: str = "Summarize: the build passed."
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt=prompt,
        task_type=task_type,
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )


@pytest.mark.usefixtures("_overlay")
@pytest.mark.parametrize("task_type", _SERVED)
def test_local_rung_route_is_first_for_every_served_class(task_type: str) -> None:
    decision = routing.delta(_request(task_type))
    assert decision.selected_backend_ref == _LEAD
    assert decision.tier_name == "local"
    assert decision.endpoint_url == _LEAD_URL
    assert decision.selected_model == _LEAD_MODEL


@pytest.mark.usefixtures("_overlay")
@pytest.mark.parametrize("task_type", _NOT_SERVED)
def test_local_rung_route_leaves_an_unnamed_class_on_the_tier_rung(
    task_type: str,
) -> None:
    decision = routing.delta(_request(task_type))
    assert decision.selected_backend_ref != _LEAD
    assert decision.tier_name == "local"


@pytest.mark.usefixtures("_overlay")
def test_local_rung_route_falls_through_for_a_prompt_over_its_window() -> None:
    decision = routing.delta(_request("summarization", "word " * 20_000))
    assert decision.selected_backend_ref != _LEAD


@pytest.mark.usefixtures("_overlay")
def test_local_rung_route_after_a_miss_lands_on_the_tiers_own_rung() -> None:
    decision = routing.delta(
        _request("summarization"),
        min_tier_name="local",
        excluded_backend_refs=frozenset({_LEAD}),
    )
    assert decision.selected_backend_ref == _TIER_RUNG
    assert decision.tier_name == "local"


@pytest.mark.usefixtures("_overlay")
def test_local_rung_route_names_the_lead_for_the_orchestrators() -> None:
    assert routing.lead_backend_refs() == frozenset({_LEAD})


def test_local_rung_route_unresolved_credential_is_not_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, _FIXTURE, credential=False)
    try:
        decision = routing.delta(_request("summarization"))
        assert decision.selected_backend_ref == _TIER_RUNG
    finally:
        _reset()


def test_local_rung_route_without_the_overlay_entry_leads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failing control: the same ladder, the entry removed from the overlay."""
    _bind(monkeypatch, _without_entry(tmp_path))
    try:
        assert routing.lead_backend_refs() == frozenset()
        for task_type in _SERVED:
            decision = routing.delta(_request(task_type))
            assert decision.selected_backend_ref == _TIER_RUNG
            assert decision.selected_model != _LEAD_MODEL
    finally:
        _reset()
