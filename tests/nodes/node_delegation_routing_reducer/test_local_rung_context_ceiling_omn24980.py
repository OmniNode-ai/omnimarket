# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the packaged ladder through its public routing reducer."""

from __future__ import annotations

import json
import random
import string
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.routing.input_token_bound import (
    LOCAL_MEMBER_INPUT_BOUNDS,
    input_token_upper_bound,
)
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

from . import dense_prompts_omn24980 as dense

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
# The ladder keeps this much of the window back for the request wrapper.
WRAPPER_MARGIN = 1024
READINGS = json.loads(
    (Path(__file__).parent / "fixtures/metered_token_readings.json").read_text()
)
# Metered input tokens of a 39-character prompt per local class, from a real
# in-process run of each class (the wrapper is the metered count minus the
# prompt's own estimated tokens).
WRAPPER_BY_CLASS: dict[str, int] = {
    task: metered - READINGS["wrapper"]["prompt_characters"] // 4
    for task, metered in READINGS["wrapper"]["metered_input_tokens_by_class"].items()
}
WRAPPER_MAX = max(WRAPPER_BY_CLASS.values())
CHARS_PER_TOKEN: dict[str, float] = READINGS["density_samples"]["chars_per_token"]
DENSE_READINGS: dict[str, Any] = READINGS["dense_prompt_readings"]["samples"]
PEER: dict[str, Any] = READINGS["spread_peer"]
# The coarse input budget in the handler's characters-over-four estimate. It is
# not the window guard; the tier's max_input_token_bound is (below).
INPUT_BUDGET = 35000
# The tier-level bound on the metered input tokens, written out, sized for the
# smallest member: the spread peer's slot (65536) less its output reserve
# (16384) and the wrapper margin (1024). local-coder and local-heavy-reasoning
# could take 64512 (131072 - 65536 - 1024); the peer takes 48128.
BOUND_CEILING = PEER["window_per_slot_tokens"] - PEER["max_tokens"] - WRAPPER_MARGIN
assert BOUND_CEILING == 48128
# The rungs' bound: served window less the output reserve and the wrapper margin.
RUNG_BOUND = SERVED_MAX_MODEL_LEN - OUTPUT_RESERVE - WRAPPER_MARGIN
assert RUNG_BOUND == 64512
# Filler text of plain 12-letter words: a word and its space bound at 7 tokens
# per 13 characters (letters at two per token, the space at one).
FILLER_WORD = "abcdefghijkl "


def filler(characters: int) -> str:
    return (FILLER_WORD * (characters // len(FILLER_WORD) + 1))[:characters]


def largest_estimate_within(bound: int) -> int:
    """The largest estimate (characters over four) whose filler the bound admits."""
    low, high = 1, bound * 4
    while low < high:
        middle = (low + high + 1) // 2
        if input_token_upper_bound(filler(middle * 4)) <= bound:
            low = middle
        else:
            high = middle - 1
    return low


# The largest request estimate each member takes (see ``request``).
X_EST_LIMIT = largest_estimate_within(RUNG_BOUND)
X_EST_LIMIT_PEER = largest_estimate_within(BOUND_CEILING)
assert X_EST_LIMIT_PEER < X_EST_LIMIT < INPUT_BUDGET


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
# A large-window rung outside the local tier that a pin reaches whether or not
# the class lists it (the unpinned route per class is LITERAL_OUTCOMES).
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
        prompt=filler(tokens * 4),
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
    for tokens in (1000, 11072, X_EST_LIMIT):
        decision = routing.delta(request(task, tokens))
        assert (decision.tier_name, decision.selected_backend_ref) == ("local", local)
        assert decision.selected_backend_ref not in pin_only
    # Past the local budget the literal table says where the prompt goes.
    for tokens in (X_EST_LIMIT + 1, 65536, 65537):
        assert_outcome(task, tokens, beyond)


def beyond_local_outcome(task: str) -> str:
    """What the public handler does with an unpinned prompt past every local budget."""
    try:
        decision = routing.delta(request(task, 65537))
    except ProtocolConfigurationError:
        return "refused"
    return f"{decision.tier_name}/{decision.selected_backend_ref}"


@pytest.mark.parametrize("task", sorted(LITERAL_OUTCOMES))
def test_each_class_takes_its_own_literal_route_beyond_the_local_tier(
    task: str,
) -> None:
    assert beyond_local_outcome(task) == LITERAL_OUTCOMES[task][1]


def test_beyond_local_routes_are_not_one_route_for_every_class() -> None:
    # Fails when every class takes the same route beyond the local tier (one
    # pin that ignores the class would pass a per-class parametrisation and
    # still be the same route everywhere).
    outcomes = {task: beyond_local_outcome(task) for task in LITERAL_OUTCOMES}
    assert len(set(outcomes.values())) > 1
    assert {"refused", f"{BEYOND}"} <= set(outcomes.values())
    # escalation and research are refused unpinned: the one rung beyond the
    # local tier that lists them is explicit_pin_only, so only a pin reaches it.
    assert outcomes["escalation"] == "refused"
    assert outcomes["research"] == "refused"
    listing = {
        model["backend_id"]
        for tier in PACKAGED_LADDER["tiers"]
        if tier["name"] != "local"
        for model in tier["models"]
        if {"escalation", "research"} & set(model.get("use_for", []))
    }
    assert listing == {"cloud-gemini-2-5-flash"}
    assert all(packaged_pin_only(backend) for backend in listing)


@pytest.mark.parametrize("task", sorted(LITERAL_OUTCOMES))
def test_a_pin_reaches_a_large_window_rung_whatever_the_class(task: str) -> None:
    # A pin bypasses class routing: it says the rung is reachable, not that the
    # class lists it. The per-class routes are the tests above.
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
    # the window the tier bound, the wrapper and the output reserve must fit.
    assert backend["endpoint_url_env"] == SERVED["endpoint_env"]
    assert model["id"] == SERVED_MODEL["id"]
    assert backend["max_tokens"] == OUTPUT_RESERVE
    assert model["max_context_tokens"] == INPUT_BUDGET
    assert RUNG_BOUND + WRAPPER_MAX + backend["max_tokens"] <= SERVED_MAX_MODEL_LEN


def test_each_member_declares_its_own_input_bound_not_one_tier_bound() -> None:
    # The ladder file declares no tier bound: a key there is a separate read that
    # fails open when a mounted ladder lacks it.
    local = next(t for t in PACKAGED_LADDER["tiers"] if t["name"] == "local")
    assert "max_input_token_bound" not in local
    # The rungs take the window less their output reserve and the wrapper margin;
    # the spread peer's slot takes less.
    assert {
        "local-coder": RUNG_BOUND,
        "local-heavy-reasoning": RUNG_BOUND,
        PEER["backend_id"]: BOUND_CEILING,
    } == dict(LOCAL_MEMBER_INPUT_BOUNDS)
    assert BOUND_CEILING < RUNG_BOUND == 64512
    assert (
        BOUND_CEILING + WRAPPER_MAX + PEER["max_tokens"]
        <= PEER["window_per_slot_tokens"]
    )
    assert RUNG_BOUND + WRAPPER_MAX + OUTPUT_RESERVE <= SERVED_MAX_MODEL_LEN
    # Negative control: the old 35000-estimate gate at the densest reading of
    # the generated code overflows the peer's slot and, for 140000 characters,
    # the served window too.
    digit_code = DENSE_READINGS["code_digit_named_140000"]
    assert digit_code["characters"] // 4 == INPUT_BUDGET
    assert (
        digit_code["metered_input_tokens"] + WRAPPER_MAX + OUTPUT_RESERVE
        > SERVED_MAX_MODEL_LEN
    )
    assert (
        digit_code["metered_input_tokens"] + WRAPPER_MAX + PEER["max_tokens"]
        > PEER["window_per_slot_tokens"]
    )


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
        routing.delta(request(task, X_EST_LIMIT + 1, backend))
    accepted = routing.delta(request(task, X_EST_LIMIT, backend))
    assert accepted.selected_backend_ref == backend
    # The handler does not clamp max_tokens: the decision carries the backend's
    # full reserve, so the tier's input bound is what keeps a request inside the
    # window. The bound of what was accepted is at most the tier bound.
    assert accepted.max_tokens == OUTPUT_RESERVE
    prompt = request(task, X_EST_LIMIT, backend).prompt
    assert input_token_upper_bound(prompt) <= RUNG_BOUND
    worst_case = RUNG_BOUND + WRAPPER_BY_CLASS[task] + accepted.max_tokens
    assert worst_case <= SERVED_MAX_MODEL_LEN


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
            request("summarization", X_EST_LIMIT, "local-heavy-reasoning")
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


def test_wrapper_is_metered_for_every_local_class_and_inside_the_margin() -> None:
    # One wrapper reading per class the local tier serves (19), not one
    # summarization prompt applied to all of them.
    assert set(WRAPPER_BY_CLASS) == {task for _, task in LOCAL_CLASSES}
    assert len(WRAPPER_BY_CLASS) == 19
    assert min(WRAPPER_BY_CLASS.values()) < WRAPPER_MAX
    assert WRAPPER_MAX == 420
    assert WRAPPER_MAX <= WRAPPER_MARGIN


def dense_request(
    task: str, prompt: str, backend: str | None = None
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        task_type=task,
        prompt=prompt,
        backend_id=backend,
    )


# The generated prompts at the old budget (140000 characters, 35000 estimated
# tokens), each with its metered reading from the lab endpoint.
AT_OLD_BUDGET = sorted(k for k in DENSE_READINGS if k.endswith("_140000"))
# Generated prompts below the tier bound, metered the same way.
BELOW_BOUND = ("code_digit_named_58000", "code_letter_named_61000")


def reading_text(key: str) -> str:
    name, _, characters = key.rpartition("_")
    return dense.sample(name, int(characters))


def metered_or_at_least(key: str) -> int:
    reading = DENSE_READINGS[key]
    return int(reading.get("metered_input_tokens") or reading["at_least_input_tokens"])


@pytest.mark.parametrize("key", sorted(DENSE_READINGS))
def test_recorded_dense_prompts_are_the_generated_texts(key: str) -> None:
    text = reading_text(key)
    assert len(text) == DENSE_READINGS[key]["characters"]
    assert dense.sha256(text) == DENSE_READINGS[key]["sha256"]


@pytest.mark.parametrize("key", sorted(DENSE_READINGS))
def test_the_bound_never_undercounts_a_metered_prompt(key: str) -> None:
    # The fixture records only what the endpoint metered; no bound computed by
    # the function under test is stored beside it.
    assert "upper_bound" not in DENSE_READINGS[key]
    bound = input_token_upper_bound(reading_text(key))
    assert bound >= metered_or_at_least(key), key


def test_the_densest_reading_is_the_digit_named_code_the_review_names() -> None:
    reading = DENSE_READINGS["code_digit_named_140000"]
    # 140000 characters meter 69445 tokens: 2.02 characters per token, not the
    # 2.21 of ledger text the old budget was fitted to, and not 4.
    assert reading["metered_input_tokens"] == 69445
    assert round(reading["characters"] / reading["metered_input_tokens"], 2) == 2.02
    assert reading["metered_input_tokens"] > (
        SERVED_MAX_MODEL_LEN - OUTPUT_RESERVE - WRAPPER_MAX
    )
    letters = DENSE_READINGS["code_letter_named_140000"]
    assert round(letters["characters"] / letters["metered_input_tokens"], 2) == 2.45
    assert DENSE_READINGS["digits_140000"]["at_least_input_tokens"] > 130000


@pytest.mark.parametrize("key", AT_OLD_BUDGET)
@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
def test_a_dense_prompt_at_the_old_budget_is_never_sent_to_a_local_rung(
    backend: str, task: str, key: str
) -> None:
    prompt = reading_text(key)
    # At the old gate (35000 estimated tokens) this prompt was accepted by the
    # rung; the endpoint then metered at least the recorded count, which with the
    # full output reserve passes the window for the digit-named code text.
    assert len(prompt) // 4 <= INPUT_BUDGET
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(dense_request(task, prompt, backend))
    try:
        decision = routing.delta(dense_request(task, prompt))
    except ProtocolConfigurationError:
        return
    assert decision.tier_name != "local", key


@pytest.mark.parametrize("key", BELOW_BOUND)
@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
def test_a_dense_prompt_below_the_bound_routes_and_fits_every_member(
    backend: str, task: str, key: str
) -> None:
    prompt = reading_text(key)
    assert input_token_upper_bound(prompt) <= BOUND_CEILING
    decision = routing.delta(dense_request(task, prompt, backend))
    assert decision.selected_backend_ref == backend
    metered = metered_or_at_least(key)
    # The rung's window and the spread peer's slot, each with its own reserve.
    assert (
        metered + WRAPPER_BY_CLASS[task] + decision.max_tokens <= SERVED_MAX_MODEL_LEN
    )
    assert (
        metered + WRAPPER_BY_CLASS[task] + PEER["max_tokens"]
        <= PEER["window_per_slot_tokens"]
    )


def test_a_prompt_one_token_over_the_rung_bound_is_refused_locally() -> None:
    # "ab " bounds at two tokens (two letters at one, the space at one).
    at_bound = "ab " * (RUNG_BOUND // 2)
    assert input_token_upper_bound(at_bound) == RUNG_BOUND
    inside = routing.delta(dense_request("summarization", at_bound, LOCAL_HEAVY))
    assert inside.selected_backend_ref == LOCAL_HEAVY
    over = at_bound + "ab "
    assert input_token_upper_bound(over) == RUNG_BOUND + 2
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(dense_request("summarization", over, LOCAL_HEAVY))


def with_spread_peer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Load the placement the lab overlay declares for the second host (spread)."""
    path = Path(__import__("os").environ["BIFROST_CONTRACT_PATH"])
    data = yaml.safe_load(path.read_text())
    heavy = next(b for b in data["backends"] if b["backend_id"] == LOCAL_HEAVY)
    peer = dict(heavy)
    peer.update(
        backend_id=PEER["backend_id"],
        model_name=PEER["backend_id"],
        max_tokens=PEER["max_tokens"],
        timeout_ms=PEER["timeout_ms"],
        placement=PEER["placement"],
    )
    data["backends"].append(peer)
    peer_path = tmp_path / "bifrost-with-peer.yaml"
    peer_path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(peer_path))
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()


def local_choices(
    task: str, prompt: str, count: int = 200
) -> tuple[set[str], set[str]]:
    """The local members chosen and the tiers chosen over ``count`` correlation ids."""
    members: set[str] = set()
    tiers: set[str] = set()
    for _ in range(count):
        try:
            decision = routing.delta(dense_request(task, prompt))
        except ProtocolConfigurationError:
            tiers.add("refused")
            continue
        tiers.add(decision.tier_name)
        if decision.tier_name == "local":
            members.add(decision.selected_backend_ref)
    return members, tiers


def test_the_loaded_placement_spreads_a_small_prompt_over_the_peer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Control: with the placement loaded the peer answers some requests (the
    # packaged ladder alone never reaches it).
    members, _ = local_choices("summarization", "x" * 4000)
    assert members == {LOCAL_HEAVY}
    with_spread_peer(monkeypatch, tmp_path)
    members, _ = local_choices("summarization", "x" * 4000)
    assert members == {LOCAL_HEAVY, PEER["backend_id"]}


@pytest.mark.parametrize("key", AT_OLD_BUDGET)
def test_a_dense_prompt_at_the_old_budget_never_reaches_the_spread_peer(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_spread_peer(monkeypatch, tmp_path)
    prompt = reading_text(key)
    # The peer's placement allows the whole 35000-estimate budget (the smaller of
    # 131072 and the rung's 35000), but its slot holds 65536 less a 16384
    # reserve: an input ceiling of 49152 against metered counts of 57171 and up.
    assert metered_or_at_least(key) > PEER["input_ceiling_tokens"] or "code" in key
    members, tiers = local_choices("summarization", prompt)
    assert members == set(), key
    assert "local" not in tiers


@pytest.mark.parametrize("key", ["code_digit_named_140000", "code_letter_named_140000"])
def test_the_old_gate_admitted_a_prompt_that_overflows_the_peer_slot(key: str) -> None:
    # Arithmetic on the recorded readings only: what the 35000 gate let through.
    metered = metered_or_at_least(key)
    assert metered + WRAPPER_MAX > PEER["input_ceiling_tokens"]
    assert metered + WRAPPER_MAX + PEER["max_tokens"] > PEER["window_per_slot_tokens"]


# ---- OMN-25320: counting, per-member gating and every selection path ----------


def random_mixed_case(characters: int, seed: int = 11) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters) for _ in range(characters))


# The lab endpoint overflows the peer's slot above 48732 metered tokens and the
# rungs' window above 65116 (the review's reading of random mixed-case letters).
PEER_OVERFLOW_TOKENS = 48732
RUNG_OVERFLOW_TOKENS = 65116
RANDOM_LETTERS = 96256  # twice the peer's bound of 48128


def test_input_token_bound_counts_random_mixed_case_letters_at_least_per_letter() -> (
    None
):
    text = random_mixed_case(RANDOM_LETTERS)
    assert len(text) == RANDOM_LETTERS == 2 * BOUND_CEILING
    assert len(set(text)) > 40
    bound = input_token_upper_bound(text)
    # The old count (two letters per token) read 48128, below the metered count.
    assert bound > RUNG_OVERFLOW_TOKENS > PEER_OVERFLOW_TOKENS > BOUND_CEILING
    assert bound >= RANDOM_LETTERS  # one token per letter, the byte-level ceiling


def test_input_token_bound_counts_a_long_lowercase_run_per_letter() -> None:
    assert input_token_upper_bound("a" * 4000) == 4000
    # A plain word and a capitalised word stay at two letters per token.
    assert input_token_upper_bound("hello") == 3
    assert input_token_upper_bound("Hello") == 3
    assert input_token_upper_bound("aBcDeF") == 6


@pytest.mark.parametrize("backend", [LOCAL_CODER, LOCAL_HEAVY])
def test_random_mixed_case_letters_are_never_sent_to_a_local_rung(
    backend: str,
) -> None:
    prompt = random_mixed_case(RANDOM_LETTERS)
    task = "code_generation" if backend == LOCAL_CODER else "summarization"
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(dense_request(task, prompt, backend))
    try:
        decision = routing.delta(dense_request(task, prompt))
    except ProtocolConfigurationError:
        return
    assert decision.tier_name != "local"


# Code at 4.4 characters per token (fixtures/metered_token_readings.json).
CODE_CHARS_PER_TOKEN = 4.4


def code_prompt(characters: int) -> str:
    return dense.sample("code_letter_named", characters)


@pytest.mark.parametrize("characters", [65000, 80000])
def test_per_member_local_coder_admits_code_above_65000_characters(
    characters: int,
) -> None:
    prompt = code_prompt(characters)
    assert len(prompt) == characters
    # 65000 characters at 4.4 per token meter about 14773 tokens, far inside the
    # rungs' window; the bound (0.72 to 0.79 per character) must not refuse them,
    # and 80000 characters (bound above the peer's 48128) are the ones the single
    # tier-wide bound sized for the peer refused.
    assert characters / CODE_CHARS_PER_TOKEN < RUNG_BOUND
    bound = input_token_upper_bound(prompt)
    assert bound <= RUNG_BOUND
    assert (characters == 80000) == (bound > BOUND_CEILING)
    decision = routing.delta(dense_request("code_generation", prompt, LOCAL_CODER))
    assert (decision.tier_name, decision.selected_backend_ref) == ("local", LOCAL_CODER)
    unpinned = routing.delta(dense_request("code_generation", prompt))
    assert (unpinned.tier_name, unpinned.selected_backend_ref) == (
        "local",
        LOCAL_CODER,
    )


def test_per_member_spread_peer_takes_only_prompts_inside_its_own_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_spread_peer(monkeypatch, tmp_path)
    inside = filler(4 * 20000)
    assert input_token_upper_bound(inside) <= BOUND_CEILING
    members, _ = local_choices("summarization", inside)
    assert members == {LOCAL_HEAVY, PEER["backend_id"]}
    between = code_prompt(80000)
    assert BOUND_CEILING < input_token_upper_bound(between) <= RUNG_BOUND
    members, tiers = local_choices("summarization", between)
    # The rung takes it; the peer, whose slot it would overflow, never does.
    assert members == {LOCAL_HEAVY}
    assert tiers == {"local"}


# Every selection path takes the prompt: the reducer, the bus-less first tier, the
# escalation hop, the tier's backend and the same-tier sibling.
OVER_RUNG = "ab " * (RUNG_BOUND // 2 + 1)


def test_tier_selection_refuses_local_for_an_over_bound_prompt_on_every_path() -> None:
    assert input_token_upper_bound(OVER_RUNG) > RUNG_BOUND
    # Positive controls: with no prompt (or a small one) every path answers local.
    assert routing.first_eligible_tier("summarization", prompt="") == "local"
    assert routing.first_eligible_tier("summarization", prompt="ab ") == "local"
    assert (
        routing.backend_id_for_tier("local", "summarization", prompt="") == LOCAL_HEAVY
    )
    assert (
        routing.backend_id_for_tier("local", "summarization", prompt="ab ")
        == LOCAL_HEAVY
    )
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "summarization", frozenset({LOCAL_HEAVY}), "ab "
        )
        is None
    )
    # The over-bound prompt: no path picks the local tier or a local member.
    assert routing.first_eligible_tier("summarization", prompt=OVER_RUNG) != "local"
    assert (
        routing.backend_id_for_tier("local", "summarization", prompt=OVER_RUNG) is None
    )
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "code_generation", frozenset({LOCAL_HEAVY}), OVER_RUNG
        )
        is None
    )
    after = routing.next_eligible_tier(
        "cheap_frontier", frozenset(), task_type="summarization", prompt=OVER_RUNG
    )
    assert after != "local"


def test_bus_less_loop_cannot_pick_local_for_an_over_bound_prompt() -> None:
    import asyncio

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port,
    )

    token = port._ACTIVE_PROMPT.set(OVER_RUNG)
    try:
        dispatcher = port.LocalDelegationDispatchPort.__new__(
            port.LocalDelegationDispatchPort
        )
        backend = dispatcher._resolve_initial_backend("summarization", spread_key="k")
    finally:
        port._ACTIVE_PROMPT.reset(token)
    assert asyncio  # the resolver is synchronous; kept to name the loop's module
    assert backend.backend_id not in {LOCAL_CODER, LOCAL_HEAVY}
    small = port._ACTIVE_PROMPT.set("ab ")
    try:
        control = dispatcher._resolve_initial_backend("summarization", spread_key="k")
    finally:
        port._ACTIVE_PROMPT.reset(small)
    assert control.backend_id == LOCAL_HEAVY


def test_an_absent_ladder_key_fails_closed_for_an_unlisted_local_member() -> None:
    # A local member the table does not list takes the smallest bound (a ladder
    # mounted without any bound key, as the dev mount is, is still bounded).
    from omnimarket.handlers.routing.input_token_bound import input_bound_members

    class Member:
        def __init__(self, backend_ref: str) -> None:
            self.backend_ref = backend_ref

    members = (Member(LOCAL_CODER), Member("local-unlisted-peer"))
    between = code_prompt(80000)
    assert BOUND_CEILING < input_token_upper_bound(between) <= RUNG_BOUND
    kept = input_bound_members("local", members, between)
    assert [m.backend_ref for m in kept] == [LOCAL_CODER]
    # Another tier is not bounded here, and an empty prompt (a probe) keeps all.
    assert input_bound_members("cheap_frontier", members, between) == members
    assert input_bound_members("local", members, "") == members


def test_tier_dev_mount_ladder_without_a_bound_key_still_refuses_over_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ladder = tmp_path / "mounted-ladder.yaml"
    ladder.write_text(ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text())
    assert "max_input_token_bound:" not in ladder.read_text()
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(ladder))
    routing._config = None
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(dense_request("summarization", OVER_RUNG, LOCAL_HEAVY))
    assert (
        routing.delta(
            dense_request("summarization", "ab ", LOCAL_HEAVY)
        ).selected_backend_ref
        == LOCAL_HEAVY
    )


def test_the_exclusion_report_names_the_input_bound_for_every_member_excluded() -> None:
    from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason

    report = routing.build_routing_exclusion_report(
        dense_request("brief_drafting", OVER_RUNG)
    )
    local = next(c for c in report.candidates if c.tier_name == "local")
    assert local.reason == EnumRoutingExclusionReason.TIER_INPUT_BOUND_EXCEEDED
    # Every member of the enum has a rendered text (no exhaustive-map gap).
    assert all(isinstance(reason.value, str) for reason in EnumRoutingExclusionReason)
