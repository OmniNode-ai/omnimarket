# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No delegation rung calls z.ai's GLM Coding Plan endpoint directly (OMN-20173).

The Coding Plan terms, section 4, bar using the plan's quota by "directly invoking
model APIs from your own applications, bots, websites, SaaS products or other
systems" (knowledge-base-internal reference/zai-glm-coding-plan-terms.md,
OMN-20154). The delegation node's direct HTTP calls to the coding surface were
exactly that, so the rungs are disabled and unplaced. GLM is used only through Claude Code.

Config-resolution level only: no network I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_CONFIGS = _ROOT / "src/omnimarket/configs"
_BIFROST = _CONFIGS / "bifrost_delegation.yaml"
_TIERS = _CONFIGS / "routing_tiers.yaml"
_RUNG_EVAL = _CONFIGS / "delegation_rung_eval.v1.yaml"

_CODING_SURFACE = "/api/coding/"
_REMOVED_BACKENDS = frozenset({"cloud-glm", "cloud-glm-5-3"})


def _coding_plan_backends(config: dict[str, Any]) -> list[str]:
    """Backend ids whose endpoint names z.ai's coding surface."""
    return [
        str(b.get("backend_id"))
        for b in config.get("backends") or []
        if _CODING_SURFACE in str(b.get("endpoint_url") or "")
    ]


def _placed_backend_ids(tiers: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            backend = node.get("backend_id")
            if isinstance(backend, str):
                found.add(backend)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tiers)
    return found


def test_the_detector_flags_a_coding_plan_backend() -> None:
    """Positive control: the check would catch the rung this ticket removed."""
    planted = {
        "backends": [
            {
                "backend_id": "planted",
                "endpoint_url": "https://api.z.ai/api/coding/paas/v4/chat/completions",
            }
        ]
    }
    assert _coding_plan_backends(planted) == ["planted"]


def test_no_backend_calls_the_coding_plan_endpoint() -> None:
    config = yaml.safe_load(_BIFROST.read_text())
    assert config.get("backends"), (
        "the routing contract lists no backends; the read is wrong"
    )
    assert _coding_plan_backends(config) == []


def test_the_glm_coding_plan_backends_are_disabled() -> None:
    """Declared (the BYOK catalogue's glm row needs a platform rung naming its slug) but with a null
    endpoint and no endpoint env, the shape the routing reducer skips, so nothing can call them."""
    backends = {
        str(b.get("backend_id")): b
        for b in yaml.safe_load(_BIFROST.read_text())["backends"]
    }
    for backend_id in _REMOVED_BACKENDS:
        backend = backends.get(backend_id)
        if backend is None:
            continue
        assert backend.get("endpoint_url") is None, backend_id
        assert not backend.get("endpoint_url_env"), backend_id


def test_no_routing_tier_places_a_removed_glm_rung() -> None:
    placed = _placed_backend_ids(yaml.safe_load(_TIERS.read_text()))
    assert placed, "no placements were read; the walk is wrong"
    assert not placed & _REMOVED_BACKENDS


def test_every_placed_backend_is_declared() -> None:
    declared = {
        str(b.get("backend_id"))
        for b in yaml.safe_load(_BIFROST.read_text())["backends"]
    }
    missing = _placed_backend_ids(yaml.safe_load(_TIERS.read_text())) - declared
    assert not missing, (
        f"routing places backends the contract does not declare: {sorted(missing)}"
    )


def test_the_nightly_rung_eval_has_no_direct_glm_rung() -> None:
    rungs = yaml.safe_load(_RUNG_EVAL.read_text()).get("rungs") or []
    assert rungs, "no rungs were read; the read is wrong"
    assert "cloud-glm" not in {str(r.get("rung_id")) for r in rungs}


def test_a_disabled_rung_still_counts_as_a_house_credential() -> None:
    """Disabling a rung by a null endpoint must not shrink the house set (INV-068).

    Positive control and the claim together: the Vertex rung carries a null endpoint in
    the committed contract too, and both its credential and the disabled GLM rungs'
    credential stay in the set a customer-path request is checked against.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    routing._load_bifrost_endpoints.cache_clear()
    try:
        refs = routing.shipped_house_credential_refs()
        bound = routing._load_bifrost_endpoints()
    finally:
        routing._load_bifrost_endpoints.cache_clear()
    assert "cloud-glm" not in bound
    assert "cloud-glm-5-3" not in bound
    assert "llm.glm.api_key" in refs
    assert "llm.gemini.api_key" in refs
