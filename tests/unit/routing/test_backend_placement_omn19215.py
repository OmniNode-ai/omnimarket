# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19215: a lane-added backend is placed in the tier ladder as a fallback.

A lane overlay may ADD a backend (OMN-17099), but routing only offers a backend
that ``routing_tiers.yaml`` names in a tier, and that file ships in this
package. A ``placement`` on the bifrost backend entry names a tier and the rungs
the backend is a fallback for. The routing authority appends one mirrored entry
per rung AFTER the tier's existing models, so the placed backend is reached
only when the rung it mirrors is unavailable or already tried.

AC1: every invalid placement fails with a ProtocolConfigurationError naming the
backend. AC2's routing half: with the rung routable the placed backend is never
selected, and with the rung tried (the transport-failure sibling probe) it is.
"""

from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
    ModelPlacedDelegationBackend,
)
from omnimarket.models.delegation.wire import parse_delegation_config_yaml
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.backend_placement import (
    apply_backend_placements,
    placement_digest,
)

pytestmark = pytest.mark.unit

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


def _placed(
    *,
    backend_id: str = "local-omnipc2-chat",
    model_name: str = "Qwen3.8-27B",
    tier: str = "local",
    fallback_for: tuple[str, ...] = ("local-coder", "local-heavy-reasoning"),
    max_context_tokens: int = 32768,
) -> ModelPlacedDelegationBackend:
    return ModelPlacedDelegationBackend(
        backend_id=backend_id,
        model_name=model_name,
        placement=ModelDelegationBackendPlacement(
            tier=tier,
            fallback_for=fallback_for,
            max_context_tokens=max_context_tokens,
        ),
    )


def test_mirrored_entries_are_appended_after_the_rungs_they_mirror() -> None:
    config = parse_delegation_config_yaml(_TIERS_YAML)
    placed = apply_backend_placements(config, (_placed(),))

    local = placed.tiers[0]
    assert [m.backend_ref for m in local.models] == [
        "local-coder",
        "local-heavy-reasoning",
        "local-omnipc2-chat",
        "local-omnipc2-chat",
    ]
    coder_mirror, reasoning_mirror = local.models[2], local.models[3]
    assert coder_mirror.id == "Qwen3.8-27B"
    assert coder_mirror.use_for == ("code_generation", "test")
    # The smaller of the rung's window (65536) and the placed backend's (32768).
    assert coder_mirror.max_context_tokens == 32768
    assert coder_mirror.fast_path_threshold_tokens == 32768
    assert reasoning_mirror.use_for == ("document", "research")
    assert reasoning_mirror.max_context_tokens == 8192
    assert reasoning_mirror.fast_path_threshold_tokens == 8192
    # Other tiers are untouched.
    assert placed.tiers[1] == config.tiers[1]


def test_no_placement_returns_the_config_unchanged() -> None:
    config = parse_delegation_config_yaml(_TIERS_YAML)
    assert apply_backend_placements(config, ()) is config
    assert placement_digest(()) is None


@pytest.mark.parametrize(
    ("backend", "fragment"),
    [
        (_placed(tier="gpu_farm"), "gpu_farm"),
        (_placed(fallback_for=("local-coder", "cloud-x")), "cloud-x"),
        (_placed(fallback_for=("local-coder", "no-such-rung")), "no-such-rung"),
        (_placed(model_name=""), "model_name"),
        (_placed(backend_id="local-coder"), "already"),
    ],
    ids=[
        "unknown-tier",
        "rung-in-another-tier",
        "unknown-rung",
        "no-served-model",
        "already-in-tier",
    ],
)
def test_invalid_placement_raises_naming_the_backend(
    backend: ModelPlacedDelegationBackend, fragment: str
) -> None:
    config = parse_delegation_config_yaml(_TIERS_YAML)
    with pytest.raises(ProtocolConfigurationError) as excinfo:
        apply_backend_placements(config, (backend,))
    message = str(excinfo.value)
    assert backend.backend_id in message
    assert fragment in message


def test_placement_digest_changes_with_the_placement() -> None:
    one = placement_digest((_placed(),))
    two = placement_digest((_placed(max_context_tokens=16384),))
    assert one is not None
    assert two is not None
    assert one != two
    assert placement_digest((_placed(),)) == one


_BIFROST_YAML = textwrap.dedent(
    """\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "http://198.51.100.10:8000/v1/chat/completions"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [code_generation]
      - backend_id: local-heavy-reasoning
        provider: local
        endpoint_url: "http://198.51.100.10:8000/v1/chat/completions"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [document]
      - backend_id: local-omnipc2-chat
        provider: local
        endpoint_url: "http://198.51.100.20:8000/v1/chat/completions"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [code_generation, document]
        placement:
          tier: local
          fallback_for: [local-coder, local-heavy-reasoning]
          max_context_tokens: 32768
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


@pytest.mark.usefixtures("_authority")
def test_routing_authority_loads_the_placement() -> None:
    local = routing._get_config().tiers[0]
    assert [m.backend_ref for m in local.models][-2:] == [
        "local-omnipc2-chat",
        "local-omnipc2-chat",
    ]


@pytest.mark.usefixtures("_authority")
def test_placed_backend_is_the_sibling_only_after_its_rung_is_tried() -> None:
    # With nothing tried, the probe offers the rung itself, never the mirror.
    assert (
        routing.sibling_backend_available_in_tier("local", "document", frozenset())
        == "local-heavy-reasoning"
    )
    # With the rung tried (a transport failure on .201), the mirror answers.
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "document", frozenset({"local-heavy-reasoning"})
        )
        == "local-omnipc2-chat"
    )
    assert (
        routing.sibling_backend_available_in_tier(
            "local", "code_generation", frozenset({"local-coder"})
        )
        == "local-omnipc2-chat"
    )


@pytest.mark.usefixtures("_authority")
def test_replay_hash_covers_the_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

    tiers_bytes = (tmp_path / "routing_tiers.yaml").read_bytes()
    placed_hash = HandlerDelegationWorkflow._routing_tiers_hash()
    assert placed_hash is not None
    assert placed_hash != hashlib.sha256(tiers_bytes).hexdigest()

    # Positive control: the same contract with the placement removed hashes to
    # the tiers file alone, exactly as before this ticket.
    unplaced = tmp_path / "bifrost_unplaced.yaml"
    unplaced_yaml = _BIFROST_YAML.replace(
        "    placement:\n"
        "      tier: local\n"
        "      fallback_for: [local-coder, local-heavy-reasoning]\n"
        "      max_context_tokens: 32768\n",
        "",
    )
    assert "placement:" not in unplaced_yaml
    unplaced.write_text(unplaced_yaml)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(unplaced))
    assert (
        HandlerDelegationWorkflow._routing_tiers_hash()
        == hashlib.sha256(tiers_bytes).hexdigest()
    )


def test_the_wire_loader_lifts_the_placement_and_the_placement_loader_reads_it(
    tmp_path: Path,
) -> None:
    """The wire model never sees ``placement``; the placement loader does.

    A released consumer's ``ModelDelegationBackendConfig`` refuses unknown keys,
    so the loader lifts ``placement`` off before validating and the wire model
    keeps its released shape.
    """
    contract = tmp_path / "bifrost.yaml"
    contract.write_text(_BIFROST_YAML)
    no_overlay = tmp_path / "no-overlay.yaml"

    config = load_bifrost_delegation_config(
        config_path=contract, overlay_path=no_overlay
    )
    assert "local-omnipc2-chat" in {b.backend_id for b in config.backends}

    placed = load_bifrost_backend_placements(
        config_path=contract, overlay_path=no_overlay
    )
    assert [p.backend_id for p in placed] == ["local-omnipc2-chat"]
    assert placed[0].model_name == "Qwen3.8-27B"
    assert placed[0].placement.fallback_for == ("local-coder", "local-heavy-reasoning")


def test_a_malformed_placement_fails_the_placement_loader_naming_the_backend(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "bifrost.yaml"
    contract.write_text(
        _BIFROST_YAML.replace("max_context_tokens: 32768", "max_context_tokens: 0")
    )
    with pytest.raises(ValueError, match="local-omnipc2-chat"):
        load_bifrost_backend_placements(
            config_path=contract, overlay_path=tmp_path / "no-overlay.yaml"
        )
