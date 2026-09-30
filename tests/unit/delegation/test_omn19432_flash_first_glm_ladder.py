# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the GLM rung is flash first, then glm-5.3, then everything else.

Operator ruling, in-session 2026-09-30 (RULING 2026-09-30T14:37:39Z, amending the
"stop using flash" ruling of 11:13:44Z and 11:14:59Z): flash may be used if it
produces good responses, and it should start with flash and escalate to the next
higher model. The delegation chain of responders is cheapest first, escalating on a
failed quality gate; a provider capacity signal (HTTP 429 code 1302) moves to the
next rung and is recorded as capacity, not quality.

This file pins the ladder as behaviour of the real resolver over the real routing
contract, not as YAML text:

* ``cheap_cloud`` resolves glm-5.3-flash first for every class flash serves;
* glm-5.3 is the next rung inside the same tier, ahead of every Gemini rung, so the
  same-tier sibling hop the dispatch port already performs lands on it after a
  quality-gate rejection or a capacity refusal;
* the glm-5.3 backend is on the Coding Plan surface, on the house key, with the
  documented settings reaching it through the inference protocol profile;
* a z.ai 1302 is a retryable capacity refusal, never a quality verdict.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from omnimarket.inference.provider_quota_policy import (
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_ROUTING_TIERS_PATH = _ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_BIFROST_PATH = _ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"

_FLASH_BACKEND = "cloud-glm"
_FLASH_MODEL = "glm-5.3-flash"
_STRONG_BACKEND = "cloud-glm-5-3"
_STRONG_MODEL = "glm-5.3"
_GEMINI_BACKENDS = ("cloud-gemini-pro", "cloud-gemini-flash")
_TIER = "cheap_cloud"


def _config() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())


def _cheap_cloud_models() -> list:
    return list(next(t for t in _config().tiers if t.name == _TIER).models)


def _synthetic_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    backends: dict[str, routing.BifrostBackendRef] = {}
    for tier in config.tiers:
        for model in tier.models:
            backends.setdefault(
                model.backend_ref,
                routing.BifrostBackendRef(
                    endpoint_url=f"https://{model.backend_ref}.contract.test/v1/chat/completions",
                    model_name=model.id,
                    timeout_ms=30_000,
                    max_tokens=4096,
                ),
            )
    return backends


def _flash_classes() -> tuple[str, ...]:
    flash = next(m for m in _cheap_cloud_models() if m.backend_ref == _FLASH_BACKEND)
    return tuple(flash.use_for)


def test_both_glm_rungs_are_declared_in_cheap_cloud() -> None:
    refs = [m.backend_ref for m in _cheap_cloud_models()]
    assert _FLASH_BACKEND in refs, "flash rung was removed from cheap_cloud"
    assert _STRONG_BACKEND in refs, "glm-5.3 rung is not declared in cheap_cloud"


def test_the_flash_rung_still_calls_flash_and_the_strong_rung_calls_glm_5_3() -> None:
    by_backend = {m.backend_ref: m for m in _cheap_cloud_models()}
    assert by_backend[_FLASH_BACKEND].id == _FLASH_MODEL
    assert by_backend[_STRONG_BACKEND].id == _STRONG_MODEL


def test_glm_5_3_is_declared_directly_after_flash_and_before_every_gemini_rung() -> (
    None
):
    refs = [m.backend_ref for m in _cheap_cloud_models()]
    assert refs.index(_STRONG_BACKEND) == refs.index(_FLASH_BACKEND) + 1
    for gemini in _GEMINI_BACKENDS:
        if gemini in refs:
            assert refs.index(_STRONG_BACKEND) < refs.index(gemini), (
                f"{gemini} is declared ahead of glm-5.3, so a flash refusal would "
                "hop to the metered Gemini rung before the stronger flat-rate GLM rung"
            )


def test_glm_5_3_serves_exactly_the_classes_flash_serves() -> None:
    """An escalation target that serves fewer classes strands the classes it lacks."""
    by_backend = {m.backend_ref: m for m in _cheap_cloud_models()}
    assert set(by_backend[_STRONG_BACKEND].use_for) == set(
        by_backend[_FLASH_BACKEND].use_for
    )


# `task_model_overrides` in task_class_contracts.v1.yaml pins these three classes
# to `gemini-2.5-flash` on purpose (OMN-14625): the pin resolves BEFORE the tier's
# declaration order, so the GLM rungs are not their first cheap_cloud choice. That
# is existing behaviour this change does not touch; the set is asserted so a new
# pin, or the removal of one, is a visible edit rather than a silent drift.
_CLASSES_PINNED_TO_GEMINI = frozenset({"code_generation", "test", "refactor"})


def _select(task_type: str, exclude: frozenset[str] = frozenset()):
    config = _config()
    backends = _synthetic_backends(config)
    cheap_cloud = next(t for t in config.tiers if t.name == _TIER)
    contract = routing._get_task_class_contract()
    return routing._select_model_for_task(
        cheap_cloud.models,
        task_type,
        0,
        backends,
        contract_model_ref=routing._get_contract_model_ref(task_type),
        exclude_backend_refs=exclude,
        contract_model_ref_is_explicit_override=routing._is_explicit_task_model_override(
            task_type, contract
        ),
    )


def _glm_ladder_classes() -> tuple[str, ...]:
    return tuple(c for c in _flash_classes() if c not in _CLASSES_PINNED_TO_GEMINI)


def test_the_classes_pinned_to_gemini_are_exactly_the_documented_set() -> None:
    contract = routing._get_task_class_contract()
    pinned = {
        c
        for c in _flash_classes()
        if routing._is_explicit_task_model_override(c, contract)
        and routing._get_contract_model_ref(c) == "gemini-2.5-flash"
    }
    assert pinned == set(_CLASSES_PINNED_TO_GEMINI)


def test_flash_resolves_first_for_every_class_the_glm_ladder_serves() -> None:
    wrong: dict[str, str] = {}
    for task_type in _glm_ladder_classes():
        selected = _select(task_type)
        if selected is None or selected.backend_ref != _FLASH_BACKEND:
            wrong[task_type] = getattr(selected, "backend_ref", None) or "none"
    assert wrong == {}, f"flash is not the first cheap_cloud rung for: {wrong}"


def test_after_a_flash_refusal_the_next_rung_is_glm_5_3_for_every_glm_ladder_class() -> (
    None
):
    """The sibling hop the dispatch port performs after a gate rejection or a 429.

    ``_select_model_for_task`` with the flash backend excluded is exactly what the
    port's same-tier sibling search resolves (``sibling_backend_available_in_tier``).
    """
    wrong: dict[str, str] = {}
    for task_type in _glm_ladder_classes():
        selected = _select(task_type, frozenset({_FLASH_BACKEND}))
        if selected is None or selected.backend_ref != _STRONG_BACKEND:
            wrong[task_type] = getattr(selected, "backend_ref", None) or "none"
    assert wrong == {}, f"glm-5.3 is not the rung after flash for: {wrong}"


def test_after_both_glm_rungs_refuse_the_ladder_falls_through_to_the_rest_of_the_tier() -> (
    None
):
    for task_type in _glm_ladder_classes():
        selected = _select(task_type, frozenset({_FLASH_BACKEND, _STRONG_BACKEND}))
        assert selected is None or selected.backend_ref not in (
            _FLASH_BACKEND,
            _STRONG_BACKEND,
        )


def _bifrost_backend(backend_id: str) -> dict:
    backends = yaml.safe_load(_BIFROST_PATH.read_text())["backends"]
    return next(b for b in backends if b["backend_id"] == backend_id)


def test_the_glm_5_3_backend_is_on_the_coding_plan_surface_with_the_house_key() -> None:
    backend = _bifrost_backend(_STRONG_BACKEND)
    flash = _bifrost_backend(_FLASH_BACKEND)
    assert backend["model_name"] == _STRONG_MODEL
    assert urlparse(backend["endpoint_url"]).path.startswith("/api/coding/paas/v4")
    assert backend["endpoint_url"] == flash["endpoint_url"]
    assert backend["secret_ref"] == flash["secret_ref"] == "llm.glm.api_key"
    assert backend["tier"] == flash["tier"] == "cheap_cloud"
    assert backend["provider"] == flash["provider"] == "glm"


def test_the_flash_backend_still_pins_flash() -> None:
    assert _bifrost_backend(_FLASH_BACKEND)["model_name"] == _FLASH_MODEL


def _zai_verdict(code: str, message: str):
    policy = load_provider_quota_policy()
    return classify_quota_response(
        endpoint_url="https://api.z.ai/api/coding/paas/v4/chat/completions",
        status_code=429,
        body={"error": {"code": code, "message": message}},
        policy=policy,
    )


def test_a_zai_1302_is_a_retryable_capacity_refusal_that_disables_nothing() -> None:
    verdict = _zai_verdict("1302", "Rate limit reached for requests")
    assert verdict is not None
    assert verdict.retryable is True
    assert verdict.provider_code == "1302"
    # Declared, not merely defaulted: the contract names the code so the next
    # reader of a receipt sees a capacity refusal and not an unmapped 429.
    assert "declared retryable" in verdict.reason


def test_a_zai_1305_overload_is_retryable_too() -> None:
    verdict = _zai_verdict(
        "1305", "The service may be temporarily overloaded, please try again later"
    )
    assert verdict is not None
    assert verdict.retryable is True
    assert verdict.provider_code == "1305"
    assert "declared retryable" in verdict.reason


def test_a_zai_1308_usage_window_cap_disables_until_its_stated_reset() -> None:
    verdict = _zai_verdict(
        "1308",
        "Usage limit reached for 5 hour. Your limit will reset at 2026-09-30 19:32:52",
    )
    assert verdict is not None
    assert verdict.retryable is False
    assert verdict.provider_code == "1308"
    assert verdict.disabled_until is not None
