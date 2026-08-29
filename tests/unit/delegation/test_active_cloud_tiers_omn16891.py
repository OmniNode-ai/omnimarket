# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GLM + the OpenRouter free coder as declared working tiers [OMN-16891].

Operator directives, 2026-08-28:

* *"openrouter can also do work for ... code"* — the genuinely-FREE OpenRouter
  coder rung is an ACTIVE tier carrying code-class delegation work, not a
  last-resort rung. Free-only stays absolute (OMN-14225 / R9-amended): never a
  paid model through the aggregator.
* *"i removed the api keys just in case"* — GLM ships DECLARED-BUT-DISABLED.
  The contract entry is correct and complete; activation is gated purely on
  ``llm.glm.api_key`` resolving. No key -> the tier is not selected and no
  probe is issued.

Everything here reads the COMMITTED contracts as data — no lane overlay, no env
binding, no live endpoint (memory ``feedback_real_dispatch_path_tests``: this
file guards DECLARATION; resolution-order proofs live in the same-tier fallback
and coverage suites).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_ROUTING_TIERS = _CONFIGS / "routing_tiers.yaml"
_BIFROST = _CONFIGS / "bifrost_delegation.yaml"
_TASK_CLASSES = _CONFIGS / "task_class_contracts.v1.yaml"

# OMN-16891: the one canonical OpenRouter env-var spelling. Live probe
# 2026-08-28: the .201 runtime host exports OPENROUTER_API_KEY (len 73) and
# defines OPEN_ROUTER_API_KEY nowhere; every deployed lane container reported
# len 0 for BOTH names because omnibase_infra's lane mappings named the
# underscored form with enable_convention_fallback=false.
_CANONICAL_OPENROUTER_ENV = "OPENROUTER_API_KEY"
_RETIRED_OPENROUTER_ENV = "OPEN_ROUTER_API_KEY"

# The code-class family. These are the task types the operator's "openrouter
# ... for code" directive covers.
_CODE_CLASSES: tuple[str, ...] = (
    "code_generation",
    "code_review",
    "refactor",
    "validator_generation",
    "test",
)

_OPENROUTER_CODER_BACKEND = "openrouter-qwen3-coder-480b"
_GLM_BACKEND = "cloud-glm"

# z.ai's STANDARD (pay-as-you-go / direct-API) surface, verbatim from
# https://docs.z.ai/api-reference/llm/chat-completion. The coding-plan surface
# (/api/coding/paas/v4/...) is a DIFFERENT product, shared with ZCode.app, and
# is what produced the 429/1310 misattributed to our own usage.
_ZAI_STANDARD_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_ZAI_CODING_PLAN_FRAGMENT = "/api/coding/paas/v4/"

# Model ids z.ai currently documents. ``glm-5-turbo`` (the contract's previous
# model_name) appears nowhere in current docs on either surface.
_DOCUMENTED_GLM_IDS: frozenset[str] = frozenset(
    {
        "glm-5.3",
        "glm-5.2",
        "glm-5.1",
        "glm-5",
        "glm-4.7",
        "glm-4.7-flash",
        "glm-4.7-flashx",
        "glm-4.6",
        "glm-4.5",
        "glm-4.5-air",
        "glm-4.5-x",
        "glm-4.5-airx",
        "glm-4.5-flash",
        "glm-4-32b-0414-128k",
    }
)

# OMN-12717 reasoning-burn: the free OpenRouter coder spent 18 reasoning tokens
# on a 16-token budget and returned preamble instead of the answer. A generous
# output budget is a CORRECTNESS constraint for this rung, not a tuning nicety.
_MIN_FREE_CODER_MAX_TOKENS = 32768


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _backends() -> dict[str, dict[str, Any]]:
    return {b["backend_id"]: b for b in _load(_BIFROST)["backends"]}


def _tiers() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _load(_ROUTING_TIERS)["tiers"]}


def _tier_order(task_type: str) -> list[str]:
    entry = _load(_TASK_CLASSES)["task_classes"][task_type]
    order: list[str] = list(
        (entry.get("escalation_policy") or {}).get("tier_order") or []
    )
    return order


def _tier_serves(tier_name: str, task_type: str) -> list[str]:
    """backend_ids in ``tier_name`` declaring ``task_type`` in use_for."""
    tier = _tiers().get(tier_name) or {}
    return [
        m["backend_id"]
        for m in tier.get("models") or []
        if task_type in (m.get("use_for") or [])
    ]


@pytest.mark.unit
class TestOpenRouterFreeCoderIsAnActiveCodeTier:
    """The free coder rung actually carries the code-class family."""

    def test_cheap_frontier_serves_every_code_class(self) -> None:
        """``cheap_frontier`` must declare all five code task types.

        Before OMN-16891 it declared only code_generation/refactor/test (plus
        reasoning/research), so ``code_review`` and ``validator_generation``
        could never reach the free rung at all.
        """
        missing = [
            task_type
            for task_type in _CODE_CLASSES
            if _OPENROUTER_CODER_BACKEND
            not in _tier_serves("cheap_frontier", task_type)
        ]
        assert not missing, (
            f"cheap_frontier's {_OPENROUTER_CODER_BACKEND} does not declare "
            f"code classes {missing} in use_for — the operator's 'openrouter "
            "can also do work for code' directive is unsatisfied for those"
        )

    def test_every_code_class_can_reach_cheap_frontier(self) -> None:
        """A class's tier_order is a CLOSED set — omission makes the tier dead.

        ``_tier_order_from_contract`` excludes any tier absent from the
        declared order, so a rung that serves the task but is not listed is
        decorative.
        """
        missing = [
            task_type
            for task_type in _CODE_CLASSES
            if "cheap_frontier" not in _tier_order(task_type)
        ]
        assert not missing, (
            f"task classes {missing} omit 'cheap_frontier' from their closed "
            "escalation_policy.tier_order, so the free coder rung is "
            "unreachable for them no matter what use_for declares"
        )

    def test_free_rung_precedes_every_paid_tier(self) -> None:
        """Free before paid (OMN-14225): cheap_frontier outranks cheap_cloud.

        This is what makes it an ACTIVE tier rather than a last-resort rung —
        a code task escalating off local hits the free coder BEFORE any
        metered tier.
        """
        paid = {"cheap_cloud", "claude"}
        for task_type in _CODE_CLASSES:
            order = _tier_order(task_type)
            assert "cheap_frontier" in order, task_type
            frontier_at = order.index("cheap_frontier")
            for paid_tier in paid & set(order):
                assert frontier_at < order.index(paid_tier), (
                    f"{task_type}: paid tier {paid_tier!r} precedes the FREE "
                    f"cheap_frontier rung in {order} — a code task would spend "
                    "money before trying the zero-cost rung"
                )

    def test_cheap_frontier_stays_zero_cost(self) -> None:
        """The rung is admitted ONLY because it is genuinely free.

        Guard against a silent-paid regression (OMN-14224/14225): if the tier
        ever carries a nonzero rate, its privileged pre-paid position becomes a
        cost bug.
        """
        tier = _tiers()["cheap_frontier"]
        assert tier["cost"]["cost_type"] == "free_local"
        assert float(tier.get("cost_per_1k_tokens", 0.0)) == 0.0

    def test_free_coder_keeps_a_generous_output_budget(self) -> None:
        """OMN-12717: a small budget makes this rung return reasoning preamble.

        Live 2026-08-28 probe: asked to "Reply with exactly: ok" under
        ``max_tokens: 16``, the model burned 18 reasoning tokens and returned
        its own deliberation instead of the answer. Under-budgeting this rung
        does not degrade quality, it produces unusable artifacts.
        """
        backend = _backends()[_OPENROUTER_CODER_BACKEND]
        assert backend["max_tokens"] >= _MIN_FREE_CODER_MAX_TOKENS, (
            f"{_OPENROUTER_CODER_BACKEND} max_tokens={backend['max_tokens']} is "
            f"below the {_MIN_FREE_CODER_MAX_TOKENS} floor this rung needs to "
            "return an artifact instead of reasoning preamble (OMN-12717)"
        )


@pytest.mark.unit
class TestOpenRouterCredentialNaming:
    """One spelling, and it is the one the runtime host actually exports."""

    def test_openrouter_backends_declare_the_canonical_env_var(self) -> None:
        offenders = {
            backend_id: backend.get("api_key_env")
            for backend_id, backend in _backends().items()
            if backend.get("secret_ref") == "llm.openrouter.api_key"
            and backend.get("api_key_env") != _CANONICAL_OPENROUTER_ENV
        }
        assert not offenders, (
            f"OpenRouter backends declare a non-canonical api_key_env: "
            f"{offenders}. Expected {_CANONICAL_OPENROUTER_ENV!r}."
        )

    def test_resolver_alias_names_the_canonical_env_var(self) -> None:
        """The store-level provider-native alias must agree with the contract.

        ``_PROVIDER_NATIVE_SECRET_ALIASES`` is the resolver-side safety net for
        call sites that do not thread ``api_key_env`` (the judge adapter). If
        it names a variable no host defines, that net catches nothing.
        """
        from omnimarket.inference.secret_store_resolver import (
            _PROVIDER_NATIVE_SECRET_ALIASES,
        )

        aliases = _PROVIDER_NATIVE_SECRET_ALIASES["llm.openrouter.api_key"]
        assert _CANONICAL_OPENROUTER_ENV in aliases, (
            f"resolver alias {aliases} omits {_CANONICAL_OPENROUTER_ENV!r}"
        )
        assert _RETIRED_OPENROUTER_ENV not in aliases, (
            f"resolver alias still carries the dead {_RETIRED_OPENROUTER_ENV!r}"
        )

    def test_no_contract_surface_declares_the_retired_spelling(self) -> None:
        """No DECLARATION may name the dead variable.

        Comment prose may still quote it — the reverted-name history is what
        keeps a future reader from "fixing" the spelling back. What must not
        survive is a live declaration, which is why this reads YAML data
        rather than raw text (the same distinction omnibase_infra's
        ``# raw-prod-bypass-ok`` annotation draws for quoted-but-inert
        signatures).
        """
        for path in (_BIFROST, _ROUTING_TIERS):
            declarations = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#") and _RETIRED_OPENROUTER_ENV in line
            ]
            assert not declarations, (
                f"{path.name} still DECLARES the dead "
                f"{_RETIRED_OPENROUTER_ENV!r}: {declarations}"
            )


@pytest.mark.unit
class TestGlmShipsDeclaredButDisabled:
    """GLM's declaration is correct; its activation is credential-gated."""

    def test_glm_points_at_the_standard_zai_surface(self) -> None:
        """The coding-plan URL is the wrong product surface.

        ``/api/coding/paas/v4/`` is the ZCode.app coding-plan product, shared
        with a co-tenant whose usage burned the 429/1310 cap this ticket
        originally misattributed to our own routing. The direct-API
        subscription this tier represents lives on the standard surface.
        """
        backend = _backends()[_GLM_BACKEND]
        assert backend["endpoint_url"] == _ZAI_STANDARD_URL
        assert _ZAI_CODING_PLAN_FRAGMENT not in backend["endpoint_url"]

    def test_glm_names_a_currently_documented_model(self) -> None:
        """``glm-5-turbo`` is absent from z.ai's current docs — a dead pin."""
        backend = _backends()[_GLM_BACKEND]
        assert backend["model_name"] in _DOCUMENTED_GLM_IDS, (
            f"{_GLM_BACKEND} pins model_name={backend['model_name']!r}, which "
            "z.ai does not currently document on either surface"
        )

    def test_glm_activation_is_credential_gated(self) -> None:
        """Absent key -> tier not selected, no probe.

        The operator removed the GLM keys. The routing reducer already gates
        selection on ``_backend_secret_available``; this asserts the GLM entry
        declares the secret_ref that gate reads, so seeding the key is the
        ONLY step needed to activate the tier.
        """
        backend = _backends()[_GLM_BACKEND]
        assert backend["secret_ref"] == "llm.glm.api_key"
        assert backend["api_key_env"] == "LLM_GLM_API_KEY"

    def test_glm_is_declared_in_a_routing_tier(self) -> None:
        """A backend no tier references is dead config, not a disabled tier.

        DECLARED-but-disabled means the rung exists and self-enables on the
        key; it does not mean the rung is absent.
        """
        referenced = {
            m["backend_id"] for tier in _tiers().values() for m in tier["models"]
        }
        assert _GLM_BACKEND in referenced, (
            f"{_GLM_BACKEND} is defined but no routing tier declares it — "
            "seeding the key would activate nothing"
        )

    def test_glm_never_routes_through_the_aggregator(self) -> None:
        """Paying OpenRouter markup for a model we hold a direct key for."""
        backend = _backends()[_GLM_BACKEND]
        assert "openrouter" not in backend["endpoint_url"].lower()
