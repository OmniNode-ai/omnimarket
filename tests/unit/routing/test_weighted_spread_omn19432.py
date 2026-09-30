# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: a placed backend declares its share and the task classes it serves.

RULING 2026-09-30T15:09:11Z (OMN-19432): no model is reserved as a fallback rung;
every usable model takes traffic in proportion to what it measures. Two things
the placement of OMN-19215 could not say are declared here.

* ``weight``: a spread peer's share of first-choice traffic against its rung's own
  1.0, so a slower or smaller host takes less than an equal split and a faster one
  more.
* ``use_for``: the task classes a backend is offered for, so a host that measured
  well on some of a rung's classes joins the rung's group for those only.

Each test has a positive control: the same fixture without the field spreads the
old way.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.enums.enum_backend_placement_mode import EnumBackendPlacementMode
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
    ModelPlacedDelegationBackend,
)
from omnimarket.models.delegation.wire import ModelDelegationConfig
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.backend_placement import (
    apply_backend_placements,
    spread_pick,
    spread_weights,
)

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
            backend_id: local-heavy-reasoning
            max_context_tokens: 65536
            use_for: [document, research, review]
            fast_path_threshold_tokens: 65536
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-x
            max_context_tokens: 65536
            use_for: [document, research, review]
        eval_before_accept: false
        max_retries: 0
    """
)


def _bifrost_yaml(placement_extra: str) -> str:
    return (
        textwrap.dedent(
            f"""\
            config_version: "1.0.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-heavy-reasoning
                provider: local
                endpoint_url: "{_RUNG_URL}"
                model_name: Qwen3.8-27B
                tier: local
                capabilities: [document]
              - backend_id: local-studio-planner
                provider: local
                endpoint_url: "{_PEER_URL}"
                model_name: gpt-oss-120b
                tier: local
                capabilities: [document]
                placement:
                  tier: local
                  fallback_for: [local-heavy-reasoning]
                  max_context_tokens: 131072
                  mode: spread
            """
        )
        + textwrap.indent(textwrap.dedent(placement_extra), "      ")
        + textwrap.dedent(
            """\
              - backend_id: cloud-x
                provider: gemini
                endpoint_url: "https://cloud.test/v1/chat/completions"
                model_name: cloud-model
                tier: frontier_api
                capabilities: [document]
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


_CLASS = """\
      {name}:
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
_TASK_CLASS_CONTRACT_YAML = "task_classes:\n" + "".join(
    _CLASS.format(name=name) for name in ("document", "research", "review")
)

_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-19432-weight-{i}") for i in range(400))


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bifrost_yaml: str) -> None:
    (tmp_path / "routing_tiers.yaml").write_text(_TIERS_YAML)
    (tmp_path / "bifrost_delegation.yaml").write_text(bifrost_yaml)
    (tmp_path / "task_class_contracts.v1.yaml").write_text(_TASK_CLASS_CONTRACT_YAML)
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(tmp_path / "routing_tiers.yaml")
    )
    monkeypatch.setenv(
        "BIFROST_CONTRACT_PATH", str(tmp_path / "bifrost_delegation.yaml")
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(tmp_path / "task_class_contracts.v1.yaml")
    )
    _reset()


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture
def _bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    yield
    _reset()


def _use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement_extra: str = ""
) -> None:
    _bind(tmp_path, monkeypatch, _bifrost_yaml(placement_extra))


def _request(correlation_id: UUID, task_type: str) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize: ok.",
        task_type=task_type,
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _share(task_type: str) -> Counter[str]:
    return Counter(
        routing.delta(_request(key, task_type)).selected_backend_ref for key in _KEYS
    )


def _placed(**placement: object) -> ModelPlacedDelegationBackend:
    fields: dict[str, object] = {
        "tier": "local",
        "fallback_for": ("local-heavy-reasoning",),
        "max_context_tokens": 131072,
        "mode": EnumBackendPlacementMode.SPREAD,
        **placement,
    }
    return ModelPlacedDelegationBackend(
        backend_id="local-studio-planner",
        model_name="gpt-oss-120b",
        placement=ModelDelegationBackendPlacement.model_validate(fields),
    )


def _ladder() -> ModelDelegationConfig:
    from omnimarket.models.delegation.wire import parse_delegation_config_yaml

    return parse_delegation_config_yaml(_TIERS_YAML)


def test_defaults_keep_the_old_placement_shape() -> None:
    placement = ModelDelegationBackendPlacement(
        tier="local", fallback_for=("a",), max_context_tokens=1
    )
    assert placement.weight == 1.0
    assert placement.use_for is None


@pytest.mark.parametrize("weight", [0, -1.0])
def test_a_weight_that_is_not_positive_is_refused(weight: float) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationBackendPlacement(
            tier="local", fallback_for=("a",), max_context_tokens=1, weight=weight
        )


def test_an_empty_use_for_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationBackendPlacement(
            tier="local", fallback_for=("a",), max_context_tokens=1, use_for=()
        )


def test_use_for_narrows_the_mirrored_entry() -> None:
    placed = apply_backend_placements(
        _ladder(), (_placed(use_for=("research", "review")),)
    )
    mirrored = next(
        m for m in placed.tiers[0].models if m.backend_ref == "local-studio-planner"
    )
    assert mirrored.use_for == ("research", "review")
    # Positive control: without use_for the mirror carries the whole rung list.
    whole = apply_backend_placements(_ladder(), (_placed(),))
    mirrored_all = next(
        m for m in whole.tiers[0].models if m.backend_ref == "local-studio-planner"
    )
    assert mirrored_all.use_for == ("document", "research", "review")


def test_a_use_for_that_shares_no_class_with_the_rung_is_refused() -> None:
    with pytest.raises(ProtocolConfigurationError, match="shares no task class"):
        apply_backend_placements(_ladder(), (_placed(use_for=("planning",)),))


def test_spread_weights_name_only_spread_placements() -> None:
    assert spread_weights((_placed(weight=2.5),)) == {"local-studio-planner": 2.5}
    assert spread_weights((_placed(mode=EnumBackendPlacementMode.FALLBACK),)) == {}


def test_spread_pick_is_stable_and_in_range() -> None:
    picks = [spread_pick(str(key), [1.0, 2.0, 1.0]) for key in _KEYS]
    assert picks == [spread_pick(str(key), [1.0, 2.0, 1.0]) for key in _KEYS]
    assert set(picks) == {0, 1, 2}
    assert spread_pick("k", [5.0]) == 0
    with pytest.raises(ValueError, match="at least one member"):
        spread_pick("k", [])
    with pytest.raises(ValueError, match="positive"):
        spread_pick("k", [1.0, 0.0])


def test_spread_pick_follows_the_weights() -> None:
    picks = Counter(spread_pick(str(key), [1.0, 3.0]) for key in _KEYS)
    assert 0.65 <= picks[1] / len(_KEYS) <= 0.85, picks
    equal = Counter(spread_pick(str(key), [1.0, 1.0]) for key in _KEYS)
    assert 0.4 <= equal[1] / len(_KEYS) <= 0.6, equal


@pytest.mark.usefixtures("_bound")
def test_delta_splits_by_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(tmp_path, monkeypatch, "      weight: 3.0\n")
    counts = _share("document")
    assert set(counts) == {"local-heavy-reasoning", "local-studio-planner"}
    planner = counts["local-studio-planner"] / len(_KEYS)
    assert 0.65 <= planner <= 0.85, counts


@pytest.mark.usefixtures("_bound")
def test_without_a_weight_the_split_is_even(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(tmp_path, monkeypatch)
    counts = _share("document")
    assert 0.4 <= counts["local-studio-planner"] / len(_KEYS) <= 0.6, counts


@pytest.mark.usefixtures("_bound")
def test_a_peer_shares_only_the_classes_it_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(tmp_path, monkeypatch, "      use_for: [research, review]\n")
    assert set(_share("document")) == {"local-heavy-reasoning"}
    for task_type in ("research", "review"):
        counts = _share(task_type)
        assert set(counts) == {"local-heavy-reasoning", "local-studio-planner"}
        assert min(counts.values()) >= 100, counts


@pytest.mark.usefixtures("_bound")
def test_a_retry_after_the_first_pick_lands_on_the_other_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(tmp_path, monkeypatch, "      weight: 3.0\n")
    members = {"local-heavy-reasoning", "local-studio-planner"}
    for key in _KEYS[:30]:
        first = routing.delta(_request(key, "document")).selected_backend_ref
        retry = routing.delta(
            _request(key, "document"),
            min_tier_name="local",
            excluded_backend_refs=frozenset({first}),
        )
        assert retry.selected_backend_ref == (members - {first}).pop()
