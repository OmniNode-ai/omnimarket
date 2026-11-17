# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the packaged ladder through its public routing reducer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.routing.routing_tiers_path import (
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    resolve_routing_tiers_path,
)
from omnimarket.models.delegation.wire import ModelDelegationRequest
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit
CONFIGS = ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent
PACKAGED_LADDER = yaml.safe_load(ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text())
PACKAGED_BACKENDS = {
    backend["backend_id"]: backend
    for backend in yaml.safe_load((CONFIGS / "bifrost_delegation.yaml").read_text())[
        "backends"
    ]
}
LOCAL_MODELS = next(
    tier["models"] for tier in PACKAGED_LADDER["tiers"] if tier["name"] == "local"
)
# Every (backend, class) pair the packaged local tier serves, read from the ladder.
LOCAL_CLASSES = tuple(
    (model["backend_id"], task) for model in LOCAL_MODELS for task in model["use_for"]
)
TASK_CLASS_CONTRACTS: dict[str, Any] = yaml.safe_load(
    (CONFIGS / "task_class_contracts.v1.yaml").read_text()
)["task_classes"]
SERVED = json.loads(
    (Path(__file__).parent / "fixtures/served_local_models.json").read_text()
)
SERVED_MAX_MODEL_LEN: int = SERVED["model"]["max_model_len"]
OUTPUT_RESERVE = 65536
INPUT_BUDGET = SERVED_MAX_MODEL_LEN - OUTPUT_RESERVE


def packaged_pin_only(backend_id: str) -> bool:
    return bool(PACKAGED_BACKENDS[backend_id].get("explicit_pin_only", False))


def expected_ordinary_outcome(task: str, tokens: int) -> str:
    """What an unpinned prompt of ``tokens`` meets once the local tier is passed.

    Read from the resolved ladder (the packaged tiers plus the placed rungs the
    routing authority appends), the class's packaged tier order and the packaged
    pin flags: a rung outside the local tier takes the prompt only when its tier
    is in the class's order, it serves the class, holds the prompt and is not
    pin-only. With no such rung the request is refused.
    """
    tier_order = TASK_CLASS_CONTRACTS[task]["escalation_policy"]["tier_order"]
    for tier in routing._get_config().tiers:
        if tier.name == "local" or tier.name not in tier_order:
            continue
        for model in tier.models:
            if (
                task in model.use_for
                and model.max_context_tokens >= tokens
                and not packaged_pin_only(model.backend_ref)
            ):
                return "routes_beyond_local"
    return "refused"


@pytest.fixture(autouse=True)
def packaged_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # Demonstrate the ambient override, then explicitly pin the packaged file.
    decoy = tmp_path / "wrong-ladder.yaml"
    decoy.write_text("tiers: []\n")
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(decoy))
    assert resolve_routing_tiers_path() == decoy
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(ROUTING_TIERS_PACKAGED_DEFAULT_PATH)
    )
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(CONFIGS / "task_class_contracts.v1.yaml")
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty-overlays"))
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    # Load real backend declarations through the normal YAML loader. Only I/O
    # bindings and credentials are replaced; the full tier ladder and every
    # packaged explicit_pin_only flag stay as declared.
    data = yaml.safe_load((CONFIGS / "bifrost_delegation.yaml").read_text())
    for backend in data["backends"]:
        backend["endpoint_url"] = "http://127.0.0.1:18742/v1/chat/completions"
        backend["model_name"] = backend["backend_id"]
        backend["secret_ref"] = None
        backend.pop("endpoint_url_env", None)
        backend.pop("model_name_env", None)
    path = tmp_path / "bifrost.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(path))
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def request(
    task: str, tokens: int, backend: str | None = None
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        task_type=task,
        prompt="x" * (tokens * 4),
        backend_id=backend,
    )


def overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ceiling: int,
    pairs: tuple[tuple[str, str], ...] = (("local-heavy-reasoning", "summarization"),),
) -> None:
    """Load a code budget overlay declaring ``ceiling`` for every pair in ``pairs``."""
    root = tmp_path / "overlays"
    path = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    ceilings: dict[str, dict[str, int]] = {}
    for backend, task in pairs:
        ceilings.setdefault(backend, {})[task] = ceiling
    data = {
        "class_budgets_s": {"summarization": 120},
        "sizing_margin": 1,
        "round_up_to_s": 1,
        "min_completed_samples": 1,
        "ceiling_s": 300,
        "input_ceilings": ceilings,
    }
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


def test_packaged_ladder_routes_recorded_prompt_despite_ambient_override() -> None:
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH
    decision = routing.delta(request("summarization", 11072))
    assert decision.tier_name == "local"
    assert decision.selected_backend_ref == "local-heavy-reasoning"
    assert decision.max_context_tokens + decision.max_tokens <= SERVED_MAX_MODEL_LEN


def test_loaded_backends_keep_every_packaged_pin_flag() -> None:
    # The I/O fixture replaces endpoints and credentials only. A rung that is
    # explicit_pin_only in the packaged contract must stay so, and one that is
    # not (both local rungs) must stay eligible without a pin.
    loaded = routing._load_bifrost_endpoints()
    assert set(loaded) == set(PACKAGED_BACKENDS)
    for backend_id in PACKAGED_BACKENDS:
        assert loaded[backend_id].explicit_pin_only is packaged_pin_only(backend_id)
    assert any(packaged_pin_only(b) for b in PACKAGED_BACKENDS)
    for model in LOCAL_MODELS:
        assert packaged_pin_only(model["backend_id"]) is False


def test_pin_only_rungs_are_never_chosen_without_a_pin() -> None:
    pin_only = {b for b in PACKAGED_BACKENDS if packaged_pin_only(b)}
    for task in {task for _, task in LOCAL_CLASSES}:
        for tokens in (1000, 65537):
            try:
                decision = routing.delta(request(task, tokens))
            except ProtocolConfigurationError:
                continue
            assert decision.selected_backend_ref not in pin_only


def test_ladder_pairs_come_from_the_packaged_local_tier() -> None:
    assert {backend for backend, _ in LOCAL_CLASSES} == {
        "local-coder",
        "local-heavy-reasoning",
    }
    assert ("local-heavy-reasoning", "summarization") in LOCAL_CLASSES
    assert len(LOCAL_CLASSES) == len(set(LOCAL_CLASSES))


@pytest.mark.parametrize("model", LOCAL_MODELS, ids=lambda m: m["backend_id"])
def test_both_local_rungs_fit_the_served_window(model: dict[str, Any]) -> None:
    backend = PACKAGED_BACKENDS[model["backend_id"]]
    # Both rungs read one endpoint; the recorded read of its served model names
    # the window the input budget and the output reserve must fit inside.
    assert backend["endpoint_url_env"] == SERVED["endpoint_env"]
    assert model["id"] == SERVED["model"]["id"]
    assert backend["max_tokens"] == OUTPUT_RESERVE
    assert model["max_context_tokens"] == INPUT_BUDGET
    assert model["max_context_tokens"] + backend["max_tokens"] <= SERVED_MAX_MODEL_LEN


@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
@pytest.mark.parametrize("with_overlay", [False, True])
def test_every_local_class_reserves_output(
    backend: str,
    task: str,
    with_overlay: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if with_overlay:
        # An overlay ceiling above the reserve, for every local pair, must not
        # widen what a rung accepts past served window minus output reserve.
        overlay(monkeypatch, tmp_path, SERVED_MAX_MODEL_LEN, LOCAL_CLASSES)
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request(task, INPUT_BUDGET + 1, backend))
    accepted = routing.delta(request(task, INPUT_BUDGET, backend))
    assert accepted.selected_backend_ref == backend
    assert accepted.max_tokens == OUTPUT_RESERVE
    assert accepted.max_context_tokens + accepted.max_tokens <= SERVED_MAX_MODEL_LEN
    # Ordinary traffic walks past the local tier too. Where no rung beyond it
    # serves the class and holds the prompt (read from the packaged ladder and
    # pin flags) the request is refused rather than overflowing a local rung.
    if expected_ordinary_outcome(task, INPUT_BUDGET + 1) == "refused":
        with pytest.raises(ProtocolConfigurationError, match="No routable backend"):
            routing.delta(request(task, INPUT_BUDGET + 1))
    else:
        ordinary = routing.delta(request(task, INPUT_BUDGET + 1))
        assert ordinary.tier_name != "local"


def test_overlay_above_the_reserve_does_not_widen_a_pinned_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay(monkeypatch, tmp_path, 131072)
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request("summarization", 65537, "local-heavy-reasoning"))
    assert (
        routing.delta(
            request("summarization", 65536, "local-heavy-reasoning")
        ).selected_backend_ref
        == "local-heavy-reasoning"
    )


@pytest.mark.parametrize("task", ["escalation", "research", "reasoning"])
@pytest.mark.parametrize("tokens", [8193, 11072, 65536, 65537, 131072])
def test_reasoning_classes_reach_large_window_rungs(task: str, tokens: int) -> None:
    if tokens <= INPUT_BUDGET:
        decision = routing.delta(request(task, tokens))
        assert decision.tier_name == "local"
        assert decision.max_context_tokens + decision.max_tokens <= SERVED_MAX_MODEL_LEN
    elif expected_ordinary_outcome(task, tokens) == "refused":
        with pytest.raises(ProtocolConfigurationError, match="No routable backend"):
            routing.delta(request(task, tokens))
    else:
        decision = routing.delta(request(task, tokens))
        assert decision.tier_name != "local"
        assert decision.max_context_tokens >= tokens


def test_overlay_gate_is_read_by_public_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert routing.delta(request("summarization", 11072)).tier_name == "local"
    overlay(monkeypatch, tmp_path, 8192)
    assert routing.delta(request("summarization", 11072)).tier_name != "local"
    overlay_path = (
        tmp_path / "overlays/node_delegation_code_budget_compute/overlay.yaml"
    )
    data = yaml.safe_load(overlay_path.read_text())
    data["input_ceilings"]["local-heavy-reasoning"]["summarization"] = 12000
    overlay_path.write_text(yaml.safe_dump(data))
    assert routing.delta(request("summarization", 11072)).tier_name == "local"
