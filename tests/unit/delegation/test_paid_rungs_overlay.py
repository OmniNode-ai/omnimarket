# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23756: paid hosted rungs join the ladder through the routing overlay.

Failing controls first: a paid rung in a free_local tier is refused, an absent
slug is refused, the switch off selects no paid rung, and a reached (or
unreadable) spending cap skips the paid tiers with a typed reason while the
ladder carries on to the next tier.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import SecretStr, ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    append_eval_arm_backends,
)
from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason
from omnimarket.handlers.routing import paid_rungs as paid_mod
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_paid_rung_burn import (
    ModelPaidRungCallRecord,
    ModelProviderCreditsReading,
)
from omnimarket.models.delegation.model_paid_rungs_overlay import (
    ModelPaidRungsOverlay,
)
from omnimarket.models.delegation.wire import parse_delegation_config_yaml
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
    _provider_reported_cost,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit

CONFIGS = Path(__file__).parents[3] / "src/omnimarket/configs"
ENDPOINT = "https://hosted.example/api/v1/chat/completions"
CREDITS_URL = "https://hosted.example/api/v1/credits"
SECRET_REF = "llm.hosted.api_key"
SWITCH_ON = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)
BASELINE = Decimal("73.50")
CAP = Decimal("20")


def _backend(backend_id: str, slug: str, tier: str) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "provider": "hosted",
        "endpoint_url": ENDPOINT,
        "model_name": slug,
        "secret_ref": SECRET_REF,
        "tier": tier,
        "timeout_ms": 300000,
        "max_tokens": 8192,
        "explicit_pin_only": False,
        "surface": "internal",
        "tenant_scope": "house",
        "capabilities": ["code_generation"],
    }


def _rung(
    backend_id: str, slug: str, tier: str, p_in: str, p_out: str
) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "slug": slug,
        "tier": tier,
        "price_in_usd_per_mtok": p_in,
        "price_out_usd_per_mtok": p_out,
        "context_length": 262144,
        "task_classes": ["code_generation", "document", "reasoning"],
        "catalog_read_at": "2026-11-11T13:26:42Z",
    }


RUNGS = (
    ("paid-cheap-a", "vendor/cheap-a", "paid_cheap", "0.05", "0.50"),
    ("paid-frontier-b", "vendor/frontier-b", "paid_frontier", "0.60", "3.50"),
)


def _paid(*, enabled: bool = True, **override: Any) -> dict[str, Any]:
    paid: dict[str, Any] = {
        "enabled": enabled,
        "spend_cap_usd": str(CAP),
        "after_tier": "cheap_cloud",
        "spend_source": {"credits_url": CREDITS_URL, "secret_ref": SECRET_REF},
        "tiers": [{"name": "paid_cheap"}, {"name": "paid_frontier"}],
        "backends": [_backend(b, s, t) for b, s, t, _, _ in RUNGS],
        "rungs": [_rung(*r) for r in RUNGS],
    }
    if enabled:
        paid["enabled_at"] = SWITCH_ON.isoformat()
        paid["usage_baseline_usd"] = str(BASELINE)
    paid.update(override)
    return paid


def _overlay_doc(paid: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "delegation_routing_overlay.v1", "paid_rungs": paid}


def _ladder():
    return parse_delegation_config_yaml((CONFIGS / "routing_tiers.yaml").read_text())


def _credits(usage: Decimal) -> ModelProviderCreditsReading:
    return ModelProviderCreditsReading(
        total_credits_usd=Decimal(100), total_usage_usd=usage, read_at=SWITCH_ON
    )


@pytest.fixture(autouse=True)
def _reset_reader() -> Iterator[None]:
    paid_mod.set_credits_reader(lambda _source: _credits(BASELINE))
    yield
    paid_mod.set_credits_reader(None)


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The shipped ladder and contract, the paid rungs overlay bound, a hosted key stored."""

    def bind(paid: dict[str, Any]) -> None:
        path = tmp_path / "routing-overlay.yaml"
        path.write_text(yaml.safe_dump(_overlay_doc(paid)), encoding="utf-8")
        monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(path))
        routing._load_bifrost_endpoints.cache_clear()
        routing._config = None

    use_local_store(monkeypatch, tmp_path)
    # The shipped contract with every rung reachable and keyless (the exemplar is
    # test_byok_escalation_omn20607), so the ladder past the paid tiers is routable.
    contract = yaml.safe_load((CONFIGS / "bifrost_delegation.yaml").read_text())
    for row in contract["backends"]:
        row["endpoint_url"] = "http://127.0.0.1:19876/v1/chat/completions"
        row["model_name"] = row.get("model_name") or "Qwen3.8-27B"
        row["explicit_pin_only"] = False
        row.pop("secret_ref", None)
        row.pop("api_key_env", None)
    contract_path = tmp_path / "bifrost.yaml"
    contract_path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=SECRET_REF,
            value=SecretStr("synthetic-test-value"),
            model="vendor/cheap-a",
            force=True,
        )
    )
    monkeypatch.setenv("ONEX_DELEGATION_ALLOW_PAID", "true")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(CONFIGS / "task_class_contracts.v1.yaml")
    )
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(CONFIGS / "routing_tiers.yaml")
    )
    routing._get_task_class_contract.cache_clear()
    yield bind
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    routing._config = None


# ---- refusals (failing controls) ------------------------------------------


def test_a_paid_rung_in_a_free_local_tier_is_refused() -> None:
    doc = _paid(tiers=[{"name": "paid_frontier"}])
    doc["rungs"][0]["tier"] = "cheap_frontier"  # free_local in the shipped ladder
    paid = ModelPaidRungsOverlay.model_validate(doc)
    with pytest.raises(ProtocolConfigurationError, match="free_local"):
        paid_mod.apply_paid_rungs(_ladder(), paid)


def test_a_paid_rung_joining_a_metered_ladder_tier_is_placed_there() -> None:
    doc = _paid(tiers=[{"name": "paid_frontier"}])
    doc["rungs"][0]["tier"] = "cheap_cloud"  # metered in the shipped ladder
    placed = paid_mod.apply_paid_rungs(
        _ladder(), ModelPaidRungsOverlay.model_validate(doc)
    )
    cheap_cloud = next(t for t in placed.tiers if t.name == "cheap_cloud")
    assert cheap_cloud.models[-1].backend_ref == "paid-cheap-a"


def test_an_absent_slug_is_refused_by_the_catalog_check() -> None:
    paid = ModelPaidRungsOverlay.model_validate(_paid())
    assert paid.absent_slugs(frozenset({"vendor/cheap-a"})) == ["vendor/frontier-b"]
    assert paid.absent_slugs(frozenset({"vendor/cheap-a", "vendor/frontier-b"})) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["backends"][0].update(explicit_pin_only=True),
            "explicit_pin_only",
        ),
        (lambda d: d["backends"][0].update(surface="customer"), "surface"),
        (lambda d: d["backends"][0].update(provider="glm"), "GLM"),
        (
            lambda d: (
                d["backends"][0].update(model_name="z-ai/glm-5.3"),
                d["rungs"][0].update(slug="z-ai/glm-5.3"),
            ),
            "GLM model",
        ),
        (lambda d: d["rungs"][0].update(slug="vendor/other"), "differs from backend"),
        (lambda d: d.pop("usage_baseline_usd"), "usage_baseline_usd"),
    ],
)
def test_the_overlay_refuses_a_rung_that_could_misbook_or_leak(mutate, message) -> None:
    doc = _paid()
    mutate(doc)
    with pytest.raises(ValidationError, match=message):
        ModelPaidRungsOverlay.model_validate(doc)


def test_a_paid_tier_name_colliding_with_the_ladder_is_refused() -> None:
    doc = _paid(tiers=[{"name": "claude"}, {"name": "paid_frontier"}])
    doc["rungs"][0]["tier"] = "claude"
    with pytest.raises(ProtocolConfigurationError, match="collide"):
        paid_mod.apply_paid_rungs(_ladder(), ModelPaidRungsOverlay.model_validate(doc))


def test_an_escalation_chain_naming_a_paid_tier_is_refused() -> None:
    doc = _overlay_doc(_paid())
    doc["escalation_chains"] = [
        {
            "task_class": "code_generation",
            "escalate_on": "acceptance_check",
            "rungs": ["paid_cheap"],
        }
    ]
    with pytest.raises(ValidationError, match="paid tier"):
        ModelDelegationRoutingOverlay.model_validate(doc)


# ---- the switch ----------------------------------------------------------


def test_switch_off_places_nothing_and_appends_nothing(bound) -> None:
    bound(_paid(enabled=False))
    paid = ModelPaidRungsOverlay.model_validate(_paid(enabled=False))
    ladder = _ladder()
    assert paid_mod.apply_paid_rungs(ladder, paid) is ladder
    assert paid.backend_rows() == []
    assert append_eval_arm_backends([]) == []
    order = routing.resolve_task_class_tier_order("code_generation")
    assert order == ("local", "cheap_frontier", "cheap_cloud", "claude")
    assert (
        routing.next_eligible_tier(
            "cheap_cloud", frozenset(), task_type="code_generation"
        )
        == "claude"
    )


def test_switch_on_places_the_paid_tiers_after_cheap_cloud(bound) -> None:
    bound(_paid())
    assert routing.resolve_task_class_tier_order("code_generation") == (
        "local",
        "cheap_frontier",
        "cheap_cloud",
        "paid_cheap",
        "paid_frontier",
        "claude",
    )
    # A class whose order lacks cheap_cloud is left as it is.
    assert routing.resolve_task_class_tier_order("complex_reasoning") == (
        "local",
        "claude",
    )
    appended = {row["backend_id"] for row in append_eval_arm_backends([])}
    assert appended == {"paid-cheap-a", "paid-frontier-b"}
    assert (
        routing.next_eligible_tier(
            "cheap_cloud", frozenset(), task_type="code_generation"
        )
        == "paid_cheap"
    )


def test_switch_on_tiers_are_metered_at_their_dearest_rung(bound) -> None:
    bound(_paid())
    tiers = {t.name: t for t in routing._get_config().tiers}
    assert tiers["paid_cheap"].cost is not None
    assert tiers["paid_cheap"].cost.cost_type.value == "metered"
    assert tiers["paid_cheap"].cost_per_1k_tokens == pytest.approx(0.0005)
    # 3.50 per 1M is above a 0.002-per-1k class ceiling: document never reaches it.
    assert tiers["paid_frontier"].cost_per_1k_tokens == pytest.approx(0.0035)
    entry = routing._task_class_entry(routing._get_task_class_contract(), "document")
    assert routing._tier_allowed_by_contract(tiers["paid_cheap"], entry)
    assert not routing._tier_allowed_by_contract(tiers["paid_frontier"], entry)


# ---- the spending cap ------------------------------------------------------


def test_cap_reached_skips_the_paid_tiers_with_a_typed_reason_and_the_ladder_continues(
    bound,
) -> None:
    bound(_paid())
    paid_mod.set_credits_reader(lambda _s: _credits(BASELINE + CAP))
    tiers = {t.name: t for t in routing._get_config().tiers}
    entry = routing._task_class_entry(
        routing._get_task_class_contract(), "code_generation"
    )
    for name in ("paid_cheap", "paid_frontier"):
        assert (
            routing._tier_exclusion_reason(tiers[name], entry)
            is EnumRoutingExclusionReason.TIER_PAID_RUNG_SPEND_CAP_REACHED
        )
    assert routing._tier_exclusion_reason(tiers["claude"], entry) is None
    assert (
        routing.next_eligible_tier(
            "cheap_cloud", frozenset(), task_type="code_generation"
        )
        == "claude"
    )


def test_cap_below_spend_admits_the_paid_tiers(bound) -> None:
    bound(_paid())
    paid_mod.set_credits_reader(lambda _s: _credits(BASELINE + CAP - Decimal("0.01")))
    tier = next(t for t in routing._get_config().tiers if t.name == "paid_cheap")
    assert paid_mod.paid_rung_gate_reason(tier) is None


def test_cap_unreadable_spend_closes_the_paid_tiers(bound) -> None:
    bound(_paid())

    def broken(_source: Any) -> ModelProviderCreditsReading:
        raise OSError("provider unreachable")

    paid_mod.set_credits_reader(broken)
    tier = next(t for t in routing._get_config().tiers if t.name == "paid_cheap")
    assert (
        paid_mod.paid_rung_gate_reason(tier)
        is EnumRoutingExclusionReason.TIER_PAID_RUNG_SPEND_UNREADABLE
    )


def test_cap_never_gates_a_tier_the_overlay_does_not_own(bound) -> None:
    bound(_paid())
    paid_mod.set_credits_reader(lambda _s: _credits(BASELINE + CAP * 2))
    claude = next(t for t in routing._get_config().tiers if t.name == "claude")
    assert paid_mod.paid_rung_gate_reason(claude) is None


# ---- booking and the provider's own cost ------------------------------------


def test_a_paid_rung_call_is_booked_at_its_declared_price(bound) -> None:
    bound(_paid())
    actual, _, _, _ = _compute_cost(
        "vendor/frontier-b",
        1_000_000,
        1_000_000,
        model_tier="paid_frontier",
        endpoint_url=ENDPOINT,
    )
    assert actual == Decimal("4.10")


def test_the_provider_reported_cost_is_booked_from_usage_cost() -> None:
    assert _provider_reported_cost({"usage": {"cost": 0.000123}}) == Decimal("0.000123")
    assert _provider_reported_cost({"usage": {"prompt_tokens": 3}}) is None
    assert _provider_reported_cost({"usage": {"cost": "n/a"}}) is None
    assert _provider_reported_cost({"usage": {"cost": -1}}) is None


# ---- the burn reading --------------------------------------------------------


def test_the_burn_reading_answers_how_fast_the_credit_goes() -> None:
    paid = ModelPaidRungsOverlay.model_validate(_paid())
    now = SWITCH_ON + timedelta(hours=4)
    credits = ModelProviderCreditsReading(
        total_credits_usd=Decimal(100),
        total_usage_usd=BASELINE + Decimal(2),
        read_at=now,
    )
    calls = [
        ModelPaidRungCallRecord(
            backend_id="paid-cheap-a",
            model_id="vendor/cheap-a",
            tokens_in=1000,
            tokens_out=200,
            booked_cost_usd=Decimal("0.00015"),
            provider_reported_cost_usd=Decimal("0.00016"),
            completed_at=SWITCH_ON + timedelta(hours=1),
        ),
        ModelPaidRungCallRecord(
            backend_id="paid-cheap-a",
            model_id="vendor/cheap-a",
            tokens_in=500,
            tokens_out=100,
            booked_cost_usd=Decimal("0.0001"),
            completed_at=SWITCH_ON + timedelta(hours=2),
        ),
        ModelPaidRungCallRecord(
            backend_id="paid-frontier-b",
            model_id="vendor/frontier-b",
            tokens_in=9,
            tokens_out=9,
            booked_cost_usd=Decimal("0.01"),
            completed_at=SWITCH_ON - timedelta(hours=1),  # before switch-on
        ),
    ]
    reading = paid_mod.build_paid_rung_burn_reading(paid, credits, calls, now=now)
    assert reading.balance_usd == Decimal("24.50")
    assert reading.spent_since_switch_on_usd == Decimal("2.00")
    assert reading.usd_per_hour == Decimal("0.5")
    assert reading.hours_until_balance_gone == Decimal("49")
    assert reading.hours_until_cap_reached == Decimal("36")
    (model,) = reading.per_model
    assert (model.model_id, model.calls, model.tokens_in, model.tokens_out) == (
        "vendor/cheap-a",
        2,
        1500,
        300,
    )
    # One call carried no provider figure, so the sum is not claimed.
    assert model.provider_reported_cost_usd is None
