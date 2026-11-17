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

from omnimarket.handlers.routing.measured_correct_ceiling import (
    measured_correct_ceiling,
)
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
# The raw GET /v1/models body of the endpoint both local rungs read (port 8000;
# the unserved port-8001 host of the contract is a different endpoint).
SERVED_MODEL: dict[str, Any] = next(
    m for m in SERVED["body"]["data"] if m["id"] == "Qwen3.8-27B"
)
SERVED_MAX_MODEL_LEN: int = SERVED_MODEL["max_model_len"]
RECORDED = json.loads(
    (Path(__file__).parent / "fixtures/delegate_stdout_local.json").read_text()
)["result"]["terminal_payload"]["payload"]
# The recorded run sent a 39-character prompt and the endpoint metered 373 input
# tokens: the rest is request wrapper the handler's characters-over-four estimate
# never counts.
RECORDED_WRAPPER_TOKENS: int = (
    RECORDED["metrics"]["input_tokens"] - len(RECORDED["prompt_text"]) // 4
)
OUTPUT_RESERVE = 65536
# The ladder keeps this much of the window back for the wrapper (the recorded
# overhead is below it).
WRAPPER_MARGIN = 1024
INPUT_BUDGET = SERVED_MAX_MODEL_LEN - OUTPUT_RESERVE - WRAPPER_MARGIN

LOCAL_CODER = "local-coder"
LOCAL_HEAVY = "local-heavy-reasoning"
BEYOND = "cheap_frontier/openrouter-north-mini-code"
# The literal outcome per class, written out and never derived from the ladder
# the handler loads: the local rung that answers a prompt that fits, and what an
# unpinned prompt above the local budget meets ("refused" or the rung that
# routes it). One entry per class the packaged local tier serves.
LITERAL_OUTCOMES: dict[str, tuple[str, str]] = {
    "brief_drafting": (LOCAL_HEAVY, "refused"),
    "code_generation": (LOCAL_CODER, BEYOND),
    "complex_reasoning": (LOCAL_HEAVY, "refused"),
    "context_compaction": (LOCAL_HEAVY, "refused"),
    "context_selection": (LOCAL_HEAVY, "refused"),
    "document": (LOCAL_HEAVY, BEYOND),
    "documentation": (LOCAL_HEAVY, BEYOND),
    "escalation": (LOCAL_HEAVY, "refused"),
    "evidence_comment": (LOCAL_HEAVY, "refused"),
    "extraction_proposer": (LOCAL_HEAVY, "refused"),
    "planning": (LOCAL_HEAVY, "refused"),
    "reasoning": (LOCAL_HEAVY, BEYOND),
    "refactor": (LOCAL_CODER, BEYOND),
    "research": (LOCAL_HEAVY, "refused"),
    "review": (LOCAL_HEAVY, BEYOND),
    "summarization": (LOCAL_HEAVY, BEYOND),
    "tail_summary": (LOCAL_HEAVY, "refused"),
    "test": (LOCAL_CODER, BEYOND),
    "validator_generation": (LOCAL_CODER, BEYOND),
}
# One route beyond the local tier for every class: pinned to a large-window rung
# outside it, which a pin reaches whether or not the class lists it.
PINNED_BEYOND_LOCAL = ("cloud-gemini-2-5-flash", "cheap_cloud")


def packaged_pin_only(backend_id: str) -> bool:
    return bool(PACKAGED_BACKENDS[backend_id].get("explicit_pin_only", False))


def assert_outcome(task: str, tokens: int, expected: str) -> None:
    """Assert the literal outcome of an unpinned request of ``tokens``."""
    if expected == "refused":
        with pytest.raises(ProtocolConfigurationError, match="No routable backend"):
            routing.delta(request(task, tokens))
        return
    decision = routing.delta(request(task, tokens))
    assert f"{decision.tier_name}/{decision.selected_backend_ref}" == expected


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
    narrowed: dict[tuple[str, str], int] | None = None,
) -> Path:
    """Load a code budget overlay declaring ``ceiling`` for every pair in ``pairs``.

    ``narrowed`` replaces the ceiling of named pairs inside the same file.
    """
    root = tmp_path / "overlays"
    path = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    ceilings: dict[str, dict[str, int]] = {}
    for backend, task in pairs:
        value = (narrowed or {}).get((backend, task), ceiling)
        ceilings.setdefault(backend, {})[task] = value
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
    return path


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


def test_literal_outcomes_cover_exactly_the_packaged_local_classes() -> None:
    assert set(LITERAL_OUTCOMES) == {task for _, task in LOCAL_CLASSES}
    for backend, task in LOCAL_CLASSES:
        assert LITERAL_OUTCOMES[task][0] == backend
    # The literal table is not vacuous: classes route beyond local and classes
    # are refused there.
    assert any(beyond == BEYOND for _, beyond in LITERAL_OUTCOMES.values())
    assert any(beyond == "refused" for _, beyond in LITERAL_OUTCOMES.values())


@pytest.mark.parametrize("task", sorted(LITERAL_OUTCOMES))
def test_unpinned_calls_route_literally_per_class_and_token_count(task: str) -> None:
    local, beyond = LITERAL_OUTCOMES[task]
    pin_only = {b for b in PACKAGED_BACKENDS if packaged_pin_only(b)}
    # A prompt that fits is answered by the class's own local rung. No error is
    # swallowed: a refusal here fails the test.
    for tokens in (1000, 11072, INPUT_BUDGET):
        decision = routing.delta(request(task, tokens))
        assert (decision.tier_name, decision.selected_backend_ref) == ("local", local)
        assert decision.selected_backend_ref not in pin_only
    # Past the local budget the literal table says where the prompt goes.
    for tokens in (INPUT_BUDGET + 1, 65536, 65537):
        assert_outcome(task, tokens, beyond)


@pytest.mark.parametrize("task", sorted(LITERAL_OUTCOMES))
def test_every_class_has_one_route_beyond_the_local_tier(task: str) -> None:
    backend, tier = PINNED_BEYOND_LOCAL
    decision = routing.delta(request(task, 100000, backend))
    assert (decision.tier_name, decision.selected_backend_ref) == (tier, backend)
    assert decision.max_context_tokens >= 100000


def test_ladder_pairs_come_from_the_packaged_local_tier() -> None:
    assert {backend for backend, _ in LOCAL_CLASSES} == {
        "local-coder",
        "local-heavy-reasoning",
    }
    assert ("local-heavy-reasoning", "summarization") in LOCAL_CLASSES
    assert len(LOCAL_CLASSES) == len(set(LOCAL_CLASSES))


def test_served_model_list_is_the_raw_body_of_the_shared_endpoint_not_port_8001() -> (
    None
):
    assert SERVED["request"] == "GET /v1/models"
    assert SERVED["http_status"] == 200
    assert SERVED["endpoint_port"] == 8000
    assert SERVED["endpoint_port"] != 8001
    assert set(SERVED["body"]) == {"object", "data"}
    assert SERVED["body"]["object"] == "list"
    assert SERVED_MODEL["object"] == "model"
    assert SERVED_MODEL["max_model_len"] == 131072
    for backend_id in (model["backend_id"] for model in LOCAL_MODELS):
        assert (
            PACKAGED_BACKENDS[backend_id]["endpoint_url_env"] == SERVED["endpoint_env"]
        )


def test_recorded_wrapper_overhead_is_measured_and_inside_the_margin() -> None:
    # 373 metered tokens for a 39-character prompt: about 364 of wrapper.
    assert RECORDED["metrics"]["input_tokens"] == 373
    assert len(RECORDED["prompt_text"]) == 39
    assert RECORDED_WRAPPER_TOKENS == 364
    assert RECORDED_WRAPPER_TOKENS <= WRAPPER_MARGIN


@pytest.mark.parametrize("model", LOCAL_MODELS, ids=lambda m: m["backend_id"])
def test_both_local_rungs_fit_the_served_window(model: dict[str, Any]) -> None:
    backend = PACKAGED_BACKENDS[model["backend_id"]]
    # Both rungs read one endpoint; the recorded read of its served model names
    # the window the input budget, the wrapper and the output reserve must fit.
    assert backend["endpoint_url_env"] == SERVED["endpoint_env"]
    assert model["id"] == SERVED_MODEL["id"]
    assert backend["max_tokens"] == OUTPUT_RESERVE
    assert model["max_context_tokens"] == INPUT_BUDGET
    window_used = (
        model["max_context_tokens"] + RECORDED_WRAPPER_TOKENS + backend["max_tokens"]
    )
    assert window_used <= SERVED_MAX_MODEL_LEN


@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
@pytest.mark.parametrize("with_overlay", [False, True])
def test_every_local_class_reserves_output_and_wrapper(
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
        assert measured_correct_ceiling(backend, task) == SERVED_MAX_MODEL_LEN
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request(task, INPUT_BUDGET + 1, backend))
    accepted = routing.delta(request(task, INPUT_BUDGET, backend))
    assert accepted.selected_backend_ref == backend
    # The handler does not clamp max_tokens: the decision carries the backend's
    # full reserve, so the input gate is what keeps a request inside the window.
    assert accepted.max_tokens == OUTPUT_RESERVE
    metered_worst_case = INPUT_BUDGET + RECORDED_WRAPPER_TOKENS + accepted.max_tokens
    assert metered_worst_case <= SERVED_MAX_MODEL_LEN


@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
def test_prompt_estimated_at_the_old_ceiling_is_refused_not_accepted(
    backend: str, task: str
) -> None:
    # 65536 estimated tokens with a 65536 max_tokens filled the 131072 window
    # before a single wrapper token was added.
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request(task, 65536, backend))


def test_overlay_above_the_reserve_does_not_widen_a_pinned_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay(monkeypatch, tmp_path, 131072)
    assert measured_correct_ceiling("local-heavy-reasoning", "summarization") == 131072
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request("summarization", 65537, "local-heavy-reasoning"))
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request("summarization", 65536, "local-heavy-reasoning"))
    assert (
        routing.delta(
            request("summarization", INPUT_BUDGET, "local-heavy-reasoning")
        ).selected_backend_ref
        == "local-heavy-reasoning"
    )


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


def test_131072_overlay_is_loaded_and_one_narrowed_pair_changes_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pair = ("local-heavy-reasoning", "summarization")
    other = ("local-heavy-reasoning", "research")
    # One overlay file declares 131072 for every local pair: it loads, the
    # loader reads each value back, and the prompt routes locally.
    path = overlay(monkeypatch, tmp_path, 131072, LOCAL_CLASSES)
    for backend, task in LOCAL_CLASSES:
        assert measured_correct_ceiling(backend, task) == 131072
    wide = routing.delta(request("summarization", 11072))
    assert (wide.tier_name, wide.selected_backend_ref) == ("local", pair[0])
    assert routing.delta(request("research", 11072)).selected_backend_ref == other[0]
    # The same file, with only one pair narrowed below the prompt: that pair's
    # outcome changes (the overlay, not the ladder, refuses it locally) and a
    # pair left at 131072 in the same file is unchanged.
    narrowed_path = overlay(
        monkeypatch, tmp_path, 131072, LOCAL_CLASSES, narrowed={pair: 8192}
    )
    assert narrowed_path == path
    assert measured_correct_ceiling(*pair) == 8192
    assert measured_correct_ceiling(*other) == 131072
    narrowed = routing.delta(request("summarization", 11072))
    assert (narrowed.tier_name, narrowed.selected_backend_ref) == (
        "cheap_frontier",
        "openrouter-north-mini-code",
    )
    unchanged = routing.delta(request("research", 11072))
    assert (unchanged.tier_name, unchanged.selected_backend_ref) == ("local", other[0])
