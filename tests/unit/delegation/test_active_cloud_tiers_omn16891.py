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

import ast
import pathlib
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
    # OMN-17427: code_review is withheld from every delegation tier.
    "refactor",
    "validator_generation",
    "test",
)

_OPENROUTER_CODER_BACKEND = "openrouter-nemotron-ultra"

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
        """``cheap_frontier`` must declare all routable code task types.

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

    def test_openrouter_backends_declare_no_env_var_fallback(self) -> None:
        """OMN-17372 INVERTS this: the canonical spelling is now no field.

        This asserted that every OpenRouter backend declared
        ``api_key_env: OPENROUTER_API_KEY`` -- OMN-16891's fix for a rung that
        was credential-dead because the field named a variable no host
        exported. That whole exercise presumed a house env var SHOULD
        authenticate the backend.

        It should not. OmniNode does not offer inference and there are no
        keyless customers on the cloud: a backend authenticates from its
        per-tenant ``secret_ref`` in the managed store, or it refuses. An
        ``api_key_env`` naming a house variable is the mechanism by which a
        keyless customer's delegation executed on our OpenRouter account, so
        the field is deleted rather than correctly spelled.

        ``secret_ref`` must survive -- deleting the fallback must not have
        stripped the backend of its real, store-resolved credential reference.
        """
        openrouter = {
            backend_id: backend
            for backend_id, backend in _backends().items()
            if backend.get("secret_ref") == "llm.openrouter.api_key"
        }
        assert openrouter, "no OpenRouter backend found; fixture drifted"

        offenders = {
            backend_id: backend["api_key_env"]
            for backend_id, backend in openrouter.items()
            if "api_key_env" in backend
        }
        assert not offenders, (
            f"OpenRouter backends still declare a house env-var fallback: "
            f"{offenders}. The field was deleted in OMN-17372 -- a customer "
            f"reaches OpenRouter on THEIR key, resolved per-tenant from the "
            f"managed store. Do not re-add it under any spelling."
        )

    def test_the_resolver_declares_no_provider_native_env_alias(self) -> None:
        """OMN-18695: the store-level alias map is GONE, not merely correct.

        This test used to assert that ``_PROVIDER_NATIVE_SECRET_ALIASES``
        spelled ``OPENROUTER_API_KEY`` the way the host does -- a check about
        getting a house env-var fallback RIGHT. OMN-17372 had already deleted
        ``api_key_env`` from the contract on the rule that a customer reaches
        a provider on THEIR key; the alias map was the same fallback surviving
        one layer down, and OMN-18695 removed it when provider references
        became local-store-only.

        Asserting its absence, rather than deleting the test with the map, is
        what stops the net being quietly re-strung by a future change that
        finds a provider key unresolvable and reaches for the environment.
        """
        from omnimarket.inference import secret_store_resolver

        assert not hasattr(secret_store_resolver, "_PROVIDER_NATIVE_SECRET_ALIASES")

        # Read the module as CODE, not as text. Prose may still name these
        # variables -- the history of the corrected spelling is what keeps a
        # future reader from "fixing" it back -- so this inspects string
        # LITERALS outside docstrings, the same data-not-text distinction the
        # sibling contract test below draws.
        tree = ast.parse(
            pathlib.Path(secret_store_resolver.__file__).read_text(encoding="utf-8")
        )
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        declarations = [
            literal
            for literal in literals
            if _CANONICAL_OPENROUTER_ENV in literal
            or _RETIRED_OPENROUTER_ENV in literal
        ]
        assert not declarations, (
            "the resolver declares a provider-native env-var name again: "
            f"{declarations}. A provider credential resolves from the local "
            "secret store (OMN-18695); it never falls back to the environment."
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


# OMN-20173: the GLM-on-the-Coding-Plan-surface class was removed. The direct GLM rungs are
# disabled because the Coding Plan terms bar direct API use from our own systems; the opposite
# premise is pinned by tests/unit/delegation/test_omn20173_no_coding_plan_rung.py.
