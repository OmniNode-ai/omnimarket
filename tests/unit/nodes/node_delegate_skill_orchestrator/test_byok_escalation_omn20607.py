# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A registered key is an unpinned responder after the platform's cheaper tiers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit
CONFIGS = Path(__file__).parents[4] / "src/omnimarket/configs"
ANSWER = "### ANSWER\n1. Inspect the current configuration before changing it.\n2. Implement the change after inspection.\n3. Run focused tests after implementation; release only when they pass."


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Use the shipped ladder and real resolver with isolated local/lab endpoints."""
    use_local_store(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEX_DELEGATION_ALLOW_PAID", "true")
    bifrost = yaml.safe_load((CONFIGS / "bifrost_delegation.yaml").read_text())
    for row in bifrost["backends"]:
        row["endpoint_url"] = "http://127.0.0.1:19876/v1/chat/completions"
        row["model_name"] = row.get("model_name") or "Qwen3.8-27B"
        row["explicit_pin_only"] = False
        row.pop("secret_ref", None)
        row.pop("api_key_env", None)
    path = tmp_path / "bifrost.yaml"
    path.write_text(yaml.safe_dump(bifrost))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(CONFIGS / "task_class_contracts.v1.yaml")
    )
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(CONFIGS / "routing_tiers.yaml")
    )
    overlay_path = tmp_path / "routing-overlay.yaml"
    overlay_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "delegation_routing_overlay.v1",
                "registered_byok_tier": {
                    "name": "customer_keys",
                    "after_tier": "cheap_cloud",
                    "cost_per_1k_tokens": 0.002,
                    "cost": {"cost_type": "metered", "rate_per_1k_usd": 0.002},
                    "models": [],
                    "max_retries": 0,
                },
            }
        )
    )
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    monkeypatch.setattr(routing, "_config", None)
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield path
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    routing._config = None


def add_key(provider: str) -> None:
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=f"llm.{provider}.api_key",
            value=SecretStr("synthetic-lab-test-value"),
            model=f"{provider}-tested-model",
            force=True,
        )
    )


class Responders:
    def __init__(self, *, quality_failure: bool = False, fail_byok: bool = False):
        self.calls: list[ModelLlmDelegationCallRequest] = []
        self.quality_failure = quality_failure
        self.fail_byok = fail_byok

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        byok = request.provider.startswith("byok-")
        succeeds = byok and not self.fail_byok
        if succeeds or self.quality_failure:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=True,
                content=ANSWER
                if succeeds
                else "### ANSWER\nI cannot help with this request. I refuse to answer.",
                tokens_in=10,
                tokens_out=40,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            content="",
            error_message="lab responder unavailable",
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
        )


def dispatch(tmp_path: Path, responders: Responders):
    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=responders,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    return asyncio.run(
        port.dispatch(
            prompt="Plan a configuration change with dependencies between inspection, implementation and tests.",
            task_type="planning",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


def test_planning_declares_a_reachable_byok_tier(profile):
    order = routing.resolve_task_class_tier_order("planning")
    assert order == ("local", "cheap_cloud", "customer_keys")
    assert routing.resolve_task_class_max_escalations("planning") >= len(order) - 1


@pytest.mark.parametrize("quality_failure", [False, True])
def test_unpinned_planning_reaches_a_registered_key(
    profile, tmp_path: Path, quality_failure: bool
):
    add_key("openrouter")
    responders = Responders(quality_failure=quality_failure)
    result = dispatch(tmp_path, responders)
    assert result["status"] == "completed", result
    tiers = list(dict.fromkeys(call.model_tier for call in responders.calls))
    assert tiers == ["local", "cheap_cloud", "customer_keys"]
    assert responders.calls[-1].provider == "byok-openrouter"
    assert responders.calls[-1].model_id == "openrouter-tested-model"


def test_without_keys_the_ladder_still_refuses(profile, tmp_path: Path):
    # Match the shipped profile's unavailable house cloud rungs. This control
    # also refused with no_higher_tier_available before a BYOK tier existed.
    bifrost = yaml.safe_load(profile.read_text())
    for row in bifrost["backends"]:
        if row.get("provider") != "local":
            row["explicit_pin_only"] = True
    profile.write_text(yaml.safe_dump(bifrost))
    routing._load_bifrost_endpoints.cache_clear()
    responders = Responders(quality_failure=True)
    result = dispatch(tmp_path, responders)
    assert result["status"] == "failed", result
    assert "no_higher_tier_available" in str(result)
    assert all(not call.provider.startswith("byok-") for call in responders.calls)
    assert {call.model_tier for call in responders.calls} == {"local"}


def test_keys_follow_registration_order_with_bounded_existing_retries(
    profile, tmp_path: Path
):
    # This reverses both alphabetical and catalogue order.
    add_key("openrouter")
    add_key("gemini")
    responders = Responders(fail_byok=True)
    result = dispatch(tmp_path, responders)
    assert result["status"] == "failed", result
    byok = [
        call.provider for call in responders.calls if call.provider.startswith("byok-")
    ]
    assert list(dict.fromkeys(byok)) == ["byok-openrouter", "byok-gemini"]
    from omnimarket.routing.byok_provider_backends import byok_backend_max_retries

    for backend_id in set(byok):
        assert byok.count(backend_id) == 1 + (byok_backend_max_retries(backend_id) or 0)


def test_planning_uses_the_registered_key_over_real_http_unpinned(
    profile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Lab profile: real skill handler, resolver, secret boundary and HTTP effect."""
    from omnimarket.models.delegation.wire.model_delegate_skill_request import (
        ModelDelegateSkillRequest,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        HandlerDelegateSkill,
    )
    from omnimarket.routing import byok_provider_backends
    from tests.chains.local.harness import LocalProviderStub, local_byok_catalogue

    provider = LocalProviderStub(model_id="openrouter-tested-model", content=ANSWER)
    provider.start()
    try:
        local_byok_catalogue(monkeypatch, tmp_path, provider.completions_url)
        add_key("openrouter")
        port = port_mod.LocalDelegationDispatchPort(
            evidence_db_path=tmp_path / "http-evidence.sqlite",
            effect_process_boundary=False,
        )
        response = asyncio.run(
            HandlerDelegateSkill(dispatch_port=port).handle(
                ModelDelegateSkillRequest(
                    prompt="Plan a configuration change with dependencies between inspection, implementation and tests.",
                    task_type="planning",
                    source="codex",
                    correlation_id=uuid4(),
                    metadata={
                        "ticket_id": "OMN-20607",
                        "caller_lane": "byok-escalation-tiers-cx-1255",
                    },
                )
            )
        )
        payload = response.model_dump(mode="json")
        assert payload["status"] == "completed", payload
        assert provider.payloads
        assert provider.payloads[-1]["model"] == "openrouter-tested-model"
        # Assert only the boolean: never include a credential in failure output.
        credential_matches = provider.presented_credential == "synthetic-lab-test-value"
        assert credential_matches
        assert list(
            dict.fromkeys(attempt["tier"] for attempt in payload["attempts"])
        ) == ["local", "cheap_cloud", "customer_keys"]
        assert payload["attempts"][-1]["backend_id"] == "byok-openrouter"
        credential_absent_from_result = "synthetic-lab-test-value" not in str(payload)
        assert credential_absent_from_result
    finally:
        provider.stop()
        byok_provider_backends.load_byok_provider_catalog.cache_clear()
        byok_provider_backends.load_byok_plan_catalog.cache_clear()
        byok_provider_backends.load_byok_not_offered_providers.cache_clear()


@pytest.mark.parametrize(
    "policy",
    ["paid_disabled", "cloud_blocked", "price_ceiling", "closed_order", "withheld"],
)
def test_registered_keys_respect_routing_policy(
    profile, monkeypatch: pytest.MonkeyPatch, policy: str
):
    from copy import deepcopy

    from omnimarket.routing.local_byok_route import registered_byok_routes

    add_key("openrouter")
    if policy == "paid_disabled":
        monkeypatch.setenv("ONEX_DELEGATION_ALLOW_PAID", "false")
    else:
        contract = deepcopy(routing._get_task_class_contract())
        entry = contract["task_classes"]["planning"]
        if policy == "cloud_blocked":
            entry["cloud_routing_policy"] = "blocked"
        elif policy == "price_ceiling":
            entry["pricing_ceiling_per_1k_tokens"] = 0.001
        elif policy == "closed_order":
            entry["escalation_policy"]["tier_order"] = ["local"]
        else:
            entry["routing_availability"] = {"status": "withheld"}
        monkeypatch.setattr(
            routing,
            "_task_class_entry",
            lambda _contract, task: contract["task_classes"].get(task),
        )
    routes = registered_byok_routes()
    assert routes
    assert (
        routing.next_eligible_tier(
            "cheap_cloud",
            frozenset(),
            task_type="planning",
            byok_backends=routes,
        )
        is None
    )


def test_deployed_resolver_never_reads_a_host_local_key(profile):
    add_key("openrouter")
    # The bus call has no local route snapshot even on a machine with keys.
    assert (
        routing.next_eligible_tier("cheap_cloud", frozenset(), task_type="planning")
        is None
    )


def test_registered_key_snapshot_honors_quota(profile):
    from datetime import UTC, datetime

    from omnimarket.inference.provider_quota_state import ModelProviderQuotaSnapshot
    from omnimarket.routing.local_byok_route import registered_byok_routes

    add_key("openrouter")
    assert registered_byok_routes()
    unknown = ModelProviderQuotaSnapshot.unknown(
        as_of=datetime.now(UTC), reason="lab read unavailable"
    )
    assert registered_byok_routes(quota_state=unknown) == ()


def test_no_overlay_keeps_the_packaged_policy(profile, monkeypatch: pytest.MonkeyPatch):
    from omnimarket.routing.local_byok_route import registered_byok_routes

    add_key("openrouter")
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH")
    assert routing.resolve_task_class_tier_order("planning") == ("local", "cheap_cloud")
    assert routing.resolve_task_class_max_escalations("planning") == 1
    assert registered_byok_routes() == ()


def test_overlay_tier_cannot_shadow_an_existing_rung(
    profile, monkeypatch: pytest.MonkeyPatch
):
    import os

    from omnibase_infra.errors import ProtocolConfigurationError

    path = Path(os.environ["DELEGATION_ROUTING_OVERLAY_PATH"])
    overlay = yaml.safe_load(path.read_text())
    overlay["registered_byok_tier"]["name"] = "local"
    path.write_text(yaml.safe_dump(overlay))
    with pytest.raises(ProtocolConfigurationError, match="collides"):
        routing.resolve_task_class_tier_order("planning")


def test_registered_tier_refuses_static_models(profile):
    from pydantic import ValidationError

    from omnimarket.models.delegation.model_delegation_routing_overlay import (
        ModelDelegationRoutingOverlay,
    )

    with pytest.raises(ValidationError, match="models come only"):
        ModelDelegationRoutingOverlay.model_validate(
            {
                "schema_version": "delegation_routing_overlay.v1",
                "registered_byok_tier": {
                    "name": "customer_keys",
                    "after_tier": "cheap_cloud",
                    "cost_per_1k_tokens": 0.002,
                    "models": [
                        {
                            "id": "unregistered-model",
                            "backend_ref": "unregistered",
                            "max_context_tokens": 1000,
                            "use_for": ["planning"],
                        }
                    ],
                },
            }
        )
