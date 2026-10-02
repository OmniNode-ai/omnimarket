# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20163: deterministic free entry shares preserve the retry ladder."""

from __future__ import annotations

import hashlib
import textwrap
from collections import Counter
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.inference.provider_quota_state import (
    ModelProviderQuotaBlock,
    ModelProviderQuotaSnapshot,
)
from omnimarket.models.delegation.model_entry_tier_share import (
    ModelEntryTierShare,
    entry_first_order,
    entry_share_draw,
    entry_share_from_contract_entry,
    validate_entry_share,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
)

pytestmark = pytest.mark.unit

_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        cost:
          cost_type: free_local
        models:
          - id: local-model
            backend_id: local-backend
            max_context_tokens: 65536
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_frontier
        cost:
          cost_type: free_local
        models:
          - id: entry-model
            backend_id: entry-backend
            max_context_tokens: 65536
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-backend
            max_context_tokens: 65536
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
    """
)

_BIFROST_YAML = textwrap.dedent(
    """\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-backend
        provider: local
        endpoint_url: "http://198.51.100.10:8000/v1/chat/completions"
        model_name: local-model
        tier: local
        capabilities: [document]
      - backend_id: entry-backend
        provider: local
        endpoint_url: "http://198.51.100.20:8000/v1/chat/completions"
        model_name: entry-model
        tier: local
        capabilities: [document]
      - backend_id: cloud-backend
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
        backend_ids: [local-backend, entry-backend, cloud-backend]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
    default_backends: [local-backend]
    """
)

_CONTRACT_YAML = textwrap.dedent(
    """\
    task_classes:
      document:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        definition_of_done:
          deterministic: [response_non_empty]
        escalation_policy:
          max_escalations: 2
          tier_order: [local, cheap_frontier, cheap_cloud]
    """
)

_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-20163-entry-{i}") for i in range(1000))
_DRAWN = next(key for key in _KEYS if entry_share_draw(key, 0.25))
_UNDRAWN = next(key for key in _KEYS if not entry_share_draw(key, 0.25))


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture(autouse=True)
def _bound() -> Generator[None, None, None]:
    yield
    _reset()


def _bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry_tier: str = "cheap_frontier",
    share: float | None = 0.25,
    tiers_yaml: str = _TIERS_YAML,
    bifrost_yaml: str = _BIFROST_YAML,
) -> None:
    contract_yaml = _CONTRACT_YAML
    if share is not None:
        contract_yaml += f"      entry_share: {{tier: {entry_tier}, share: {share}}}\n"
    for name, contents in (
        ("routing_tiers.yaml", tiers_yaml),
        ("bifrost_delegation.yaml", bifrost_yaml),
        ("task_class_contracts.v1.yaml", contract_yaml),
    ):
        (tmp_path / name).write_text(contents)
    for name, filename in (
        ("DELEGATION_ROUTING_TIERS_PATH", "routing_tiers.yaml"),
        ("BIFROST_CONTRACT_PATH", "bifrost_delegation.yaml"),
        ("BIFROST_OVERLAY_PATH", "no-overlay.yaml"),
        ("TASK_CLASS_CONTRACT_PATH", "task_class_contracts.v1.yaml"),
    ):
        monkeypatch.setenv(name, str(tmp_path / filename))
    _reset()


def _request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize: ok.",
        task_type="document",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _picks(quota_state: ModelProviderQuotaSnapshot | None = None) -> Counter[str]:
    return Counter(
        routing.delta(_request(key), quota_state=quota_state).selected_backend_ref
        for key in _KEYS
    )


def _quota_snapshot(provider_id: str) -> ModelProviderQuotaSnapshot:
    return ModelProviderQuotaSnapshot(
        tenant_id=None,
        as_of=datetime.now(UTC),
        readable=True,
        blocks=(
            ModelProviderQuotaBlock(
                credential_ref="unauthenticated",
                provider_id=provider_id,
                model_scope="*",
                disposition="disable_until_billing",
                blocked_indefinitely=True,
            ),
        ),
    )


def test_entry_tier_share_fraction_and_absent_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    counts = _picks()
    assert set(counts) == {"local-backend", "entry-backend"}
    assert abs(counts["entry-backend"] / len(_KEYS) - 0.25) <= 0.05, counts
    for key in _KEYS[:30]:
        assert (
            routing.first_eligible_tier("document", correlation_id=key)
            == routing.delta(_request(key)).tier_name
        )
    _bind(tmp_path, monkeypatch, share=None)
    assert _picks() == Counter({"local-backend": len(_KEYS)})


@pytest.mark.parametrize("share", [0.0, 1.0])
def test_entry_tier_share_extremes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, share: float
) -> None:
    _bind(tmp_path, monkeypatch, share=share)
    expected = "entry-backend" if share == 1.0 else "local-backend"
    assert _picks() == Counter({expected: len(_KEYS)})


@pytest.mark.parametrize("tier", ["unknown", "local", "cheap_cloud"])
def test_entry_tier_share_validation_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    _bind(tmp_path, monkeypatch, entry_tier=tier)
    with pytest.raises(ProtocolConfigurationError, match="OMN-20163"):
        routing.delta(_request(_UNDRAWN))
    with pytest.raises(ProtocolConfigurationError, match="OMN-20163"):
        routing.first_eligible_tier("document")
    with pytest.raises(ProtocolConfigurationError, match="OMN-20163"):
        routing.next_eligible_tier("local", frozenset(), task_type="document")


def test_entry_tier_share_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    for key in (_DRAWN, _UNDRAWN):
        first = routing.delta(_request(key)).tier_name
        assert routing.delta(_request(key)).tier_name == first
        _reset()
        assert routing.delta(_request(key)).tier_name == first
        raw = hashlib.sha256(f"entry-tier-share:{key}".encode()).digest()
        fresh_draw = int.from_bytes(raw[:8], "big") / 2**64 < 0.25
        assert entry_share_draw(key, 0.25) == fresh_draw
        assert entry_share_draw(str(key), 0.25) == fresh_draw
        assert (first == "cheap_frontier") == fresh_draw


def test_entry_tier_share_draw_and_order() -> None:
    assert not entry_share_draw(_DRAWN, -0.1)
    assert not entry_share_draw(_DRAWN, 0.0)
    assert entry_share_draw(_UNDRAWN, 1.0)
    assert entry_share_draw(_UNDRAWN, 1.1)
    assert entry_first_order(
        ("local", "cheap_frontier", "cheap_cloud"), "cheap_frontier"
    ) == ("cheap_frontier", "local", "cheap_cloud")


@pytest.mark.parametrize(
    "block",
    [
        None,
        "bad",
        {},
        {"tier": ""},
        {"tier": "x", "share": -0.1},
        {"tier": "x", "share": 1.1},
        {"tier": "x", "share": float("nan")},
        {"tier": "x", "share": 0.25, "extra": True},
    ],
)
def test_entry_tier_share_malformed_block(block: object) -> None:
    with pytest.raises(ProtocolConfigurationError, match="OMN-20163"):
        entry_share_from_contract_entry({"escalation_policy": {"entry_share": block}})


def test_entry_tier_share_optional_and_frozen() -> None:
    entries: tuple[dict[str, object] | None, ...] = (
        None,
        {},
        {"escalation_policy": {}},
        {"escalation_policy": None},
    )
    for entry in entries:
        assert entry_share_from_contract_entry(entry) is None
    assert (
        entry_share_from_contract_entry(
            {"escalation_policy": {"entry_share": {"tier": "x", "share": 0.0}}}
        )
        is None
    )
    share = entry_share_from_contract_entry(
        {"escalation_policy": {"entry_share": {"tier": "x", "share": 0.25}}}
    )
    assert share == ModelEntryTierShare(tier="x", share=0.25)
    assert share is not None
    with pytest.raises(ValidationError, match="frozen"):
        share.share = 0.5
    with pytest.raises(ProtocolConfigurationError, match="OMN-20163"):
        validate_entry_share(share, (), lambda _: True)


def test_entry_tier_share_backend_pin_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch, share=1.0)
    request = _request(_DRAWN).model_copy(update={"backend_id": "local-backend"})
    assert routing.delta(request).selected_backend_ref == "local-backend"
    assert (
        routing.delta(request, min_tier_name="cheap_frontier").tier_name
        == "cheap_frontier"
    )


def test_entry_miss_retries_local_before_metered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    assert routing.delta(_request(_DRAWN)).tier_name == "cheap_frontier"
    assert (
        routing.next_eligible_tier(
            "cheap_frontier",
            frozenset({"cheap_frontier"}),
            task_type="document",
            correlation_id=_DRAWN,
        )
        == "local"
    )
    assert (
        routing.next_eligible_tier(
            "local",
            frozenset({"cheap_frontier", "local"}),
            task_type="document",
            correlation_id=_DRAWN,
        )
        == "cheap_cloud"
    )
    assert (
        routing.delta(_request(_DRAWN), min_tier_name="local").selected_backend_ref
        == "local-backend"
    )
    assert (
        routing.delta(
            _request(_DRAWN), min_tier_name="cheap_cloud"
        ).selected_backend_ref
        == "cloud-backend"
    )
    assert (
        routing.next_eligible_tier(
            "cheap_cloud",
            frozenset({"cheap_frontier", "local", "cheap_cloud"}),
            task_type="document",
            correlation_id=_DRAWN,
        )
        is None
    )


@pytest.mark.parametrize("correlation_id", [None, _UNDRAWN])
def test_entry_miss_retries_local_undrawn_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, correlation_id: UUID | None
) -> None:
    _bind(tmp_path, monkeypatch)
    assert (
        routing.next_eligible_tier(
            "cheap_frontier",
            frozenset({"cheap_frontier"}),
            task_type="document",
            correlation_id=correlation_id,
        )
        == "cheap_cloud"
    )
    assert (
        routing.next_eligible_tier(
            "local",
            frozenset({"local"}),
            task_type="document",
            correlation_id=correlation_id,
        )
        == "cheap_frontier"
    )


def test_entry_miss_retries_local_diagnostic_uses_drawn_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    reason = routing.describe_no_higher_tier_available(
        "cheap_frontier",
        frozenset({"local", "cheap_cloud"}),
        task_type="document",
        correlation_id=_DRAWN,
    )
    assert "tier_order=['cheap_frontier', 'local', 'cheap_cloud']" in reason
    assert "local(excluded), cheap_cloud(excluded)" in reason
    old_reason = routing.describe_no_higher_tier_available(
        "cheap_frontier", frozenset({"local", "cheap_cloud"}), task_type="document"
    )
    assert "tier_order=['local', 'cheap_frontier', 'cheap_cloud']" in old_reason
    assert "local(excluded)" not in old_reason


def test_entry_share_quota_bound_all_blocked_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    blocked = _quota_snapshot("host:198.51.100.20")
    assert routing.quota_blocked_backend_refs(blocked) == frozenset({"entry-backend"})
    assert _picks(blocked) == Counter({"local-backend": len(_KEYS)})
    assert (
        routing.first_eligible_tier(
            "document", correlation_id=_DRAWN, quota_state=blocked
        )
        == "local"
    )
    empty = ModelProviderQuotaSnapshot.empty(as_of=datetime.now(UTC))
    restored = _picks(empty)
    assert abs(restored["entry-backend"] / len(_KEYS) - 0.25) <= 0.05
    assert restored == _picks()
    assert (
        routing.first_eligible_tier(
            "document", correlation_id=_DRAWN, quota_state=empty
        )
        == "cheap_frontier"
    )


def test_entry_share_quota_bound_unrelated_backend_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    blocked = _quota_snapshot("host:cloud.test")
    assert routing.quota_blocked_backend_refs(blocked) == frozenset({"cloud-backend"})
    assert _picks(blocked) == _picks()


def test_entry_share_quota_bound_retry_ladder_ignores_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(tmp_path, monkeypatch)
    blocked = _quota_snapshot("host:198.51.100.20")
    # Starting at local after an entry miss must never offer entry again, even
    # when the quota snapshot has changed since the initial drawn pick.
    retry = routing.delta(
        _request(_DRAWN),
        min_tier_name="local",
        excluded_backend_refs=frozenset({"local-backend"}),
        quota_state=blocked,
    )
    assert retry.selected_backend_ref == "cloud-backend"
    empty = ModelProviderQuotaSnapshot.empty(as_of=datetime.now(UTC))
    assert (
        routing.delta(
            _request(_DRAWN),
            min_tier_name="local",
            excluded_backend_refs=frozenset({"local-backend"}),
            quota_state=empty,
        ).selected_backend_ref
        == "cloud-backend"
    )


def test_entry_share_quota_bound_one_unblocked_backend_keeps_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiers_yaml = _TIERS_YAML.replace(
        "      - id: entry-model\n",
        "      - id: entry-peer-model\n"
        "        backend_id: entry-peer\n"
        "        max_context_tokens: 65536\n"
        "        use_for: [document]\n"
        "      - id: entry-model\n",
    )
    bifrost_yaml = _BIFROST_YAML.replace(
        "  - backend_id: entry-backend\n",
        "  - backend_id: entry-peer\n"
        "    provider: local\n"
        '    endpoint_url: "http://198.51.100.30:8000/v1/chat/completions"\n'
        "    model_name: entry-peer-model\n"
        "    tier: local\n"
        "    capabilities: [document]\n"
        "  - backend_id: entry-backend\n",
    )
    _bind(tmp_path, monkeypatch, tiers_yaml=tiers_yaml, bifrost_yaml=bifrost_yaml)
    counts = _picks(_quota_snapshot("host:198.51.100.20"))
    assert set(counts) == {"local-backend", "entry-peer"}
    assert abs(counts["entry-peer"] / len(_KEYS) - 0.25) <= 0.05


@pytest.mark.parametrize(
    "contract_path",
    [TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH],
    ids=["entry_tier_share_shipped_contract"],
)
def test_entry_share_defaults_to_zero_for_every_shipped_class(
    contract_path: Path,
) -> None:
    contract = yaml.safe_load(contract_path.read_text())
    classes = contract["task_classes"]
    assert classes
    for name, entry in classes.items():
        assert entry_share_from_contract_entry(entry) is None, name


def test_entry_miss_retries_local_port_threads_the_correlation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process port hands the run's id to both ladder helpers."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port_mod,
    )

    seen: dict[str, dict[str, object]] = {}

    def _first(task_type: str, **kwargs: object) -> None:
        seen["first"] = kwargs

    def _next(current: str, excluded: frozenset[str], **kwargs: object) -> None:
        seen["next"] = kwargs

    monkeypatch.setattr(port_mod, "first_eligible_tier", _first)
    monkeypatch.setattr(port_mod, "next_eligible_tier", _next)
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: object()
    )
    port = port_mod.LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / "evidence.sqlite", effect_process_boundary=False
    )
    port._resolve_initial_backend("document", spread_key="run-1", quota_state=None)
    assert seen["first"]["correlation_id"] == "run-1"
    assert (
        port._resolve_next_backend(
            current_tier="cheap_frontier",
            task_type="document",
            excluded_tiers=frozenset({"cheap_frontier"}),
            spread_key="run-1",
        )
        is None
    )
    assert seen["next"]["correlation_id"] == "run-1"
