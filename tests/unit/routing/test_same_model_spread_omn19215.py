# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19215 AC4: a spread-placed backend shares its rung's first-choice traffic.

RULING ledger:4257 (OMN-18925): the lab serves the same model on more than one
host, so delegation should use them. A ``fallback`` placement reaches the
added backend only when its rung fails; a ``spread`` placement also puts it in
the rung's first-choice group, and the routing reducer picks one member per
request by a stable hash of the correlation id.

What must hold, and what each test pins:

* spreading happens only on ``delta``'s first choice, and each correlation id
  always lands on the same member (replay and retries agree);
* both members take a real share of many correlation ids;
* a transport-failure retry that excludes the member tried first lands on the
  other member, whichever was first;
* a peer that cannot take the prompt (context) or is not routable is skipped;
* the availability probes keep their ordered answers;
* a ``fallback`` placement, or a config not loaded by the routing authority,
  never spreads (the positive control for every assertion above).
"""

from __future__ import annotations

import textwrap
from collections import Counter
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from omnimarket.enums.enum_backend_placement_mode import EnumBackendPlacementMode
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
    ModelPlacedDelegationBackend,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.backend_placement import spread_groups, spread_index

pytestmark = pytest.mark.unit

_RUNG_URL = "http://198.51.100.10:8000/v1/chat/completions"
_PEER_URL = "http://198.51.100.20:8000/v1/chat/completions"

_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: Qwen3.8-27B
            backend_id: local-coder
            max_context_tokens: 65536
            use_for: [code_generation, test]
            fast_path_threshold_tokens: 65536
          - id: Qwen3.8-27B
            backend_id: local-heavy-reasoning
            max_context_tokens: 8192
            use_for: [document, research]
            fast_path_threshold_tokens: 8192
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-x
            max_context_tokens: 65536
            use_for: [code_generation, document, research, test]
        eval_before_accept: false
        max_retries: 0
    """
)


def _bifrost_yaml(mode: str | None, peer_window: int = 32768) -> str:
    mode_line = f"      mode: {mode}\n" if mode is not None else ""
    return (
        textwrap.dedent(
            f"""\
            config_version: "1.0.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-coder
                provider: local
                endpoint_url: "{_RUNG_URL}"
                model_name: Qwen3.8-27B
                tier: local
                capabilities: [code_generation]
              - backend_id: local-heavy-reasoning
                provider: local
                endpoint_url: "{_RUNG_URL}"
                model_name: Qwen3.8-27B
                tier: local
                capabilities: [document]
              - backend_id: local-omnipc2-chat
                provider: local
                endpoint_url: "{_PEER_URL}"
                model_name: Qwen3.8-27B
                tier: local
                capabilities: [code_generation, document]
                placement:
                  tier: local
                  fallback_for: [local-coder, local-heavy-reasoning]
                  max_context_tokens: {peer_window}
            """
        )
        + mode_line
        + textwrap.dedent(
            """\
              - backend_id: cloud-x
                provider: gemini
                endpoint_url: "https://cloud.test/v1/chat/completions"
                model_name: cloud-model
                tier: frontier_api
                capabilities: [code_generation, document]
            routing_rules:
              - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
                priority: 10
                task_class: document
                task_class_contract_version: "1.0.0"
                backend_policy_version: "1.0.0"
                match_operation_types: [chat_completion]
                match_capabilities: [document]
                backend_ids: [local-heavy-reasoning, cloud-x]
                fallback_policy:
                  action: escalate_to_next_tier
                  max_retries: 1
                  on_exhaust: return_error
                shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
            default_backends:
              - local-heavy-reasoning
            """
        )
    )


_TASK_CLASS_CONTRACT_YAML = textwrap.dedent(
    """\
    task_classes:
      document:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        definition_of_done:
          deterministic:
            - response_non_empty
        escalation_policy:
          max_escalations: 1
          tier_order:
            - local
            - cheap_cloud
      code_generation:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        definition_of_done:
          deterministic:
            - response_non_empty
        escalation_policy:
          max_escalations: 1
          tier_order:
            - local
            - cheap_cloud
    """
)

_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-19215-spread-{i}") for i in range(200))


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bifrost_yaml: str) -> None:
    tiers_path = tmp_path / "routing_tiers.yaml"
    tiers_path.write_text(_TIERS_YAML)
    bifrost_path = tmp_path / "bifrost_delegation.yaml"
    bifrost_path.write_text(bifrost_yaml)
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(_TASK_CLASS_CONTRACT_YAML)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers_path))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_path))
    _reset()


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture
def _spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    _bind(tmp_path, monkeypatch, _bifrost_yaml("spread"))
    yield
    _reset()


@pytest.fixture
def _fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    _bind(tmp_path, monkeypatch, _bifrost_yaml(None))
    yield
    _reset()


def _request(
    correlation_id: UUID, task_type: str = "document", prompt: str = "Summarize: ok."
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt=prompt,
        task_type=task_type,
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _placed(mode: EnumBackendPlacementMode) -> ModelPlacedDelegationBackend:
    return ModelPlacedDelegationBackend(
        backend_id="local-omnipc2-chat",
        model_name="Qwen3.8-27B",
        placement=ModelDelegationBackendPlacement(
            tier="local",
            fallback_for=("local-coder", "local-heavy-reasoning"),
            max_context_tokens=32768,
            mode=mode,
        ),
    )


def test_spread_groups_name_only_spread_placements() -> None:
    assert spread_groups((_placed(EnumBackendPlacementMode.FALLBACK),)) == {}
    assert spread_groups((_placed(EnumBackendPlacementMode.SPREAD),)) == {
        "local-coder": ("local-omnipc2-chat",),
        "local-heavy-reasoning": ("local-omnipc2-chat",),
    }


def test_the_default_mode_is_fallback() -> None:
    placement = ModelDelegationBackendPlacement(
        tier="local", fallback_for=("local-coder",), max_context_tokens=1
    )
    assert placement.mode is EnumBackendPlacementMode.FALLBACK


def test_spread_index_is_stable_in_range_and_balanced() -> None:
    picks = [spread_index(str(key), 2) for key in _KEYS]
    assert set(picks) == {0, 1}
    assert picks == [spread_index(str(key), 2) for key in _KEYS]
    ones = sum(picks)
    assert 70 <= ones <= 130, f"200 keys split {200 - ones}/{ones}"
    assert all(spread_index(str(key), 1) == 0 for key in _KEYS[:10])
    with pytest.raises(ValueError, match="at least one member"):
        spread_index("k", 0)


@pytest.mark.usefixtures("_spread")
@pytest.mark.parametrize("task_type", ["document", "code_generation"])
def test_delta_spreads_first_choice_across_both_hosts(task_type: str) -> None:
    decisions = [routing.delta(_request(key, task_type)) for key in _KEYS]
    by_ref = Counter(d.selected_backend_ref for d in decisions)
    rung = "local-heavy-reasoning" if task_type == "document" else "local-coder"
    assert set(by_ref) == {rung, "local-omnipc2-chat"}
    assert min(by_ref.values()) >= 70, by_ref
    for decision in decisions:
        expected_url = (
            _PEER_URL
            if decision.selected_backend_ref == "local-omnipc2-chat"
            else _RUNG_URL
        )
        assert decision.endpoint_url == expected_url
        assert decision.tier_name == "local"


@pytest.mark.usefixtures("_spread")
def test_a_correlation_id_always_lands_on_the_same_member() -> None:
    for key in _KEYS[:20]:
        first = routing.delta(_request(key)).selected_backend_ref
        assert all(
            routing.delta(_request(key)).selected_backend_ref == first for _ in range(3)
        )


@pytest.mark.usefixtures("_spread")
def test_a_retry_that_excludes_the_member_tried_first_lands_on_the_other() -> None:
    members = {"local-heavy-reasoning", "local-omnipc2-chat"}
    seen_first: set[str] = set()
    for key in _KEYS[:40]:
        first = routing.delta(_request(key)).selected_backend_ref
        seen_first.add(first)
        retry = routing.delta(
            _request(key),
            min_tier_name="local",
            excluded_backend_refs=frozenset({first}),
        )
        assert retry.selected_backend_ref == (members - {first}).pop()
        assert retry.tier_name == "local"
    # Both directions were exercised, not only "rung failed, peer answered".
    assert seen_first == members


@pytest.mark.usefixtures("_spread")
def test_a_prompt_larger_than_the_peer_window_stays_on_the_rung() -> None:
    # ~40k tokens: inside local-coder's 65536, outside the peer's 32768.
    big = "word " * 40_000
    refs = {
        routing.delta(_request(key, "code_generation", big)).selected_backend_ref
        for key in _KEYS[:30]
    }
    assert refs == {"local-coder"}


@pytest.mark.usefixtures("_spread")
def test_the_availability_probes_keep_their_ordered_answers() -> None:
    assert (
        routing.sibling_backend_available_in_tier("local", "document", frozenset())
        == "local-heavy-reasoning"
    )
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "document", frozenset({"local-heavy-reasoning"})
        )
        == "local-omnipc2-chat"
    )


@pytest.mark.usefixtures("_fallback")
def test_a_fallback_placement_never_takes_first_choice_traffic() -> None:
    refs = {routing.delta(_request(key)).selected_backend_ref for key in _KEYS}
    assert refs == {"local-heavy-reasoning"}
    retry = routing.delta(
        _request(_KEYS[0]),
        min_tier_name="local",
        excluded_backend_refs=frozenset({"local-heavy-reasoning"}),
    )
    assert retry.selected_backend_ref == "local-omnipc2-chat"


@pytest.mark.usefixtures("_spread")
def test_a_config_not_loaded_by_the_routing_authority_never_spreads() -> None:
    loaded = routing._get_config()
    # Same ladder, but a distinct object installed the way tests install one.
    routing._config = loaded.model_copy()
    refs = {routing.delta(_request(key)).selected_backend_ref for key in _KEYS[:50]}
    assert refs == {"local-heavy-reasoning"}


@pytest.mark.usefixtures("_spread")
def test_an_explicit_backend_pin_is_not_spread() -> None:
    refs = {
        routing.delta(
            _request(key).model_copy(update={"backend_id": "local-heavy-reasoning"})
        ).selected_backend_ref
        for key in _KEYS[:50]
    }
    assert refs == {"local-heavy-reasoning"}
