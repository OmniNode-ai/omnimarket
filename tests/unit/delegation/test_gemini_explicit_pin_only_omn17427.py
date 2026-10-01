# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gemini test backends remain executable only through a caller pin."""

from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.delegation_backend_resolution import (
    _select_backend,
    _select_backend_by_id,
)
from tests.unit.delegation.test_exact_backend_pin_routing_omn15539 import (
    _request,
)
from tests.unit.delegation.test_exact_backend_pin_routing_omn15539 import (
    exact_pin_routing as exact_pin_routing,
)


def test_direct_resolution_excludes_pin_only_backend_but_preserves_pin() -> None:
    gemini = {
        "backend_id": "gemini-test",
        "endpoint_url": "https://gemini.test/v1/chat/completions",
        "capabilities": ["summarization"],
        "explicit_pin_only": True,
    }
    assert _select_backend([gemini], "summarization") is None
    assert _select_backend_by_id([gemini], "gemini-test") == gemini


def test_typed_pin_policy_and_routing_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration = ModelDelegationBackendConfig(
        backend_id="gemini-test",
        provider="gemini",
        tier="cheap_cloud",
        explicit_pin_only=True,
    )
    backend = routing.BifrostBackendRef(
        endpoint_url="https://gemini.test/v1/chat/completions",
        model_name="gemini-test",
        timeout_ms=30000,
        max_tokens=1024,
        explicit_pin_only=declaration.explicit_pin_only,
    )
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _: True)
    assert not routing._backend_routable(backend)
    assert routing._backend_routable(backend, explicit_pin=True)


@pytest.mark.usefixtures("exact_pin_routing")
def test_explicit_caller_pin_executes_a_pin_only_backend() -> None:
    routing._load_bifrost_endpoints()["caller-pin"].explicit_pin_only = True
    decision = routing.delta(_request(backend_id="caller-pin"))
    assert decision.selected_backend_ref == "caller-pin"


@pytest.mark.usefixtures("exact_pin_routing")
def test_routing_diagnosis_names_pin_only_exclusion() -> None:
    backends = routing._load_bifrost_endpoints()
    backends["cloud-fallback"].explicit_pin_only = True
    model = routing._get_config().tiers[1].models[0]
    reason, _ = routing._candidate_exclusion_reason(
        model,
        task_type="research",
        estimated_tokens=10,
        bifrost_backends=backends,
        excluded_backend_refs=frozenset(),
        quota_state=None,
    )
    assert reason is not None
    assert reason.value == "backend_requires_explicit_pin"


def test_shipped_gemini_backends_are_pin_only_and_absent_from_default_rules() -> None:
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    config = yaml.safe_load(path.read_text())
    gemini_ids = {
        backend["backend_id"]
        for backend in config["backends"]
        if backend.get("provider") in {"gemini", "vertex"}
    }
    assert gemini_ids
    for backend in config["backends"]:
        if backend["backend_id"] in gemini_ids:
            assert backend.get("explicit_pin_only") is True
    for rule in config["routing_rules"]:
        assert not gemini_ids.intersection(rule["backend_ids"])
    assert not gemini_ids.intersection(config["default_backends"])


def test_default_quality_judge_uses_a_declared_non_gemini_backend() -> None:
    from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.adapter_routing_resolved_judge import (
        _DEFAULT_JUDGE_BACKEND_ID,
    )

    config = yaml.safe_load(
        Path("src/omnimarket/configs/bifrost_delegation.yaml").read_text()
    )
    judge = next(
        b for b in config["backends"] if b["backend_id"] == _DEFAULT_JUDGE_BACKEND_ID
    )
    assert judge["provider"] not in {"gemini", "vertex"}
    assert "judge_adequacy" in judge["capabilities"]
