# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: a larger-window fallback backend takes the prompts a rung excludes.

The lab serves gpt-oss-120b with a 131072-token window beside a Qwen rung whose
routing window is 8192. Placed as a ``fallback``, its mirrored entry used to be
clamped to the rung's 8192, so a 20000-token prompt found no local backend and
went to a metered cloud tier, or, pinned to the rung, failed outright (four
worktree-triage runs, 2026-09-30, 9930 to 102925 tokens).

What must hold, and what each test pins:

* a fallback mirror carries the placed backend's own window, and keeps the
  rung's fast-path threshold, so short prompts still go to the rung first;
* a spread mirror still takes the smaller window (positive control: without it
  the fallback case above could pass on a change that widened every mode);
* through the routing authority, a prompt over the rung's window selects the
  wide backend, a short prompt still selects the rung, the wide backend is the
  sibling after the rung is tried, and a prompt over even the wide window
  selects nothing local, so it still escalates.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest

from omnimarket.enums.enum_backend_placement_mode import EnumBackendPlacementMode
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
    ModelPlacedDelegationBackend,
)
from omnimarket.models.delegation.wire import (
    ModelTierModel,
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.backend_placement import apply_backend_placements

pytestmark = pytest.mark.unit

_RUNG = "local-heavy-reasoning"
_WIDE = "local-studio-planner"
_WIDE_MODEL = "gpt-oss-120b"
_RUNG_WINDOW = 8192
_WIDE_WINDOW = 131072

_TIERS_YAML = textwrap.dedent(
    f"""\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: Qwen3.8-27B
            backend_id: {_RUNG}
            max_context_tokens: {_RUNG_WINDOW}
            use_for: [document, research, review]
            fast_path_threshold_tokens: {_RUNG_WINDOW}
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-x
            max_context_tokens: 1000000
            use_for: [document, research, review]
        eval_before_accept: false
        max_retries: 0
    """
)

_BIFROST_YAML = textwrap.dedent(
    f"""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: {_RUNG}
        provider: local
        endpoint_url: "http://198.51.100.10:8000/v1/chat/completions"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [document]
      - backend_id: {_WIDE}
        provider: local
        endpoint_url: "http://198.51.100.30:8130/v1/chat/completions"
        model_name: {_WIDE_MODEL}
        tier: local
        capabilities: [document]
        placement:
          tier: local
          fallback_for: [{_RUNG}]
          max_context_tokens: {_WIDE_WINDOW}
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
        backend_ids: [{_RUNG}, cloud-x]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
    default_backends:
      - {_RUNG}
    """
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
    """
)


def _wide(mode: EnumBackendPlacementMode) -> ModelPlacedDelegationBackend:
    return ModelPlacedDelegationBackend(
        backend_id=_WIDE,
        model_name=_WIDE_MODEL,
        placement=ModelDelegationBackendPlacement(
            tier="local",
            fallback_for=(_RUNG,),
            max_context_tokens=_WIDE_WINDOW,
            mode=mode,
        ),
    )


def _mirror_of(mode: EnumBackendPlacementMode) -> ModelTierModel:
    config = parse_delegation_config_yaml(_TIERS_YAML)
    placed = apply_backend_placements(config, (_wide(mode),))
    return next(m for m in placed.tiers[0].models if m.backend_ref == _WIDE)


def test_a_fallback_mirror_carries_the_placed_backends_own_window() -> None:
    mirror = _mirror_of(EnumBackendPlacementMode.FALLBACK)
    assert mirror.id == _WIDE_MODEL
    assert mirror.max_context_tokens == _WIDE_WINDOW
    # Short prompts still prefer the rung: the fast-path threshold stays its own.
    assert mirror.fast_path_threshold_tokens == _RUNG_WINDOW
    assert mirror.use_for == ("document", "research", "review")


def test_a_spread_mirror_keeps_the_smaller_window() -> None:
    """POSITIVE CONTROL: only the fallback mode widens."""
    mirror = _mirror_of(EnumBackendPlacementMode.SPREAD)
    assert mirror.max_context_tokens == _RUNG_WINDOW


def test_a_fallback_window_below_the_rungs_is_not_raised_to_it() -> None:
    config = parse_delegation_config_yaml(_TIERS_YAML)
    narrow = ModelPlacedDelegationBackend(
        backend_id=_WIDE,
        model_name=_WIDE_MODEL,
        placement=ModelDelegationBackendPlacement(
            tier="local", fallback_for=(_RUNG,), max_context_tokens=4096
        ),
    )
    placed = apply_backend_placements(config, (narrow,))
    mirror = next(m for m in placed.tiers[0].models if m.backend_ref == _WIDE)
    assert mirror.max_context_tokens == 4096
    assert mirror.fast_path_threshold_tokens == 4096


@pytest.fixture
def _authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind the real routing authority to the fixture contracts above."""
    tiers_path = tmp_path / "routing_tiers.yaml"
    tiers_path.write_text(_TIERS_YAML)
    bifrost_path = tmp_path / "bifrost_delegation.yaml"
    bifrost_path.write_text(_BIFROST_YAML)
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(_TASK_CLASS_CONTRACT_YAML)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers_path))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_path))
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _select(estimated_tokens: int, exclude: frozenset[str] = frozenset()) -> str | None:
    local = routing._get_config().tiers[0]
    chosen = routing._select_model_for_task(
        local.models,
        "document",
        estimated_tokens,
        routing._load_bifrost_endpoints(),
        exclude_backend_refs=exclude,
    )
    return None if chosen is None else chosen.backend_ref


@pytest.mark.usefixtures("_authority")
def test_a_short_prompt_still_goes_to_the_rung_first() -> None:
    assert _select(3000) == _RUNG
    assert _select(_RUNG_WINDOW) == _RUNG


@pytest.mark.usefixtures("_authority")
def test_a_prompt_over_the_rungs_window_goes_to_the_wide_backend() -> None:
    assert _select(_RUNG_WINDOW + 1) == _WIDE
    assert _select(100_000) == _WIDE
    assert _select(_WIDE_WINDOW) == _WIDE


@pytest.mark.usefixtures("_authority")
def test_a_prompt_over_every_local_window_selects_no_local_backend() -> None:
    """The ladder still escalates: the wide window is a ceiling, not a bypass."""
    assert _select(_WIDE_WINDOW + 1) is None


@pytest.mark.usefixtures("_authority")
def test_the_wide_backend_is_the_sibling_once_the_rung_is_tried() -> None:
    assert _select(3000, frozenset({_RUNG})) == _WIDE
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "document", frozenset({_RUNG})
        )
        == _WIDE
    )
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "document", frozenset({_RUNG, _WIDE})
        )
        is None
    )
