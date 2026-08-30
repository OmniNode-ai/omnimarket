# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GLM endpoint + served-model authority (OMN-6790).

This account holds an ACTIVE z.ai **GLM Coding Plan**. z.ai serves that plan at
ONE surface, and it is not the surface the docs' "base URL" line names:

    Coding Plan (ours)   https://api.z.ai/api/coding/paas/v4
    Pay-as-you-go        https://api.z.ai/api/paas/v4

Presenting a Coding-Plan key to the pay-as-you-go surface returns
``429 {"error":{"code":"1113","message":"Insufficient balance or no resource
package. Please recharge."}}`` — which reads as a funding problem and is not
one. That misreading has now cost three separate rediscoveries (OMN-14625
recorded the route as credential-dead; OMN-16891 repointed the backend onto the
pay-as-you-go surface and recorded the resulting 1113 as a billing gap; a third
session filed a quota-funding ticket off the same error).

The fact kept getting dropped because it lived in a memory note and an env
override instead of in the contract with a test. This module is that test. The
contract is the authority; this pins it.

Evidence (live, from the .201 host, 2026-08-30, one key sha12 ``27fecebdd647``,
one-token completion per cell):

    surface                              model          result
    https://api.z.ai/api/coding/paas/v4  glm-5.3        HTTP 200 (echo glm-5.3)
    https://api.z.ai/api/coding/paas/v4  glm-5-turbo    HTTP 200 (echo glm-5-turbo)
    https://api.z.ai/api/coding/paas/v4  glm-5.3-flash  HTTP 200 (echo glm-5.3-flash)
    https://api.z.ai/api/coding/paas/v4  glm-4.6        HTTP 200 (echo glm-4.6)
    https://api.z.ai/api/paas/v4         all four       HTTP 429 code 1113

Config-resolution level only — no network I/O is performed here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BIFROST_PATH = _PROJECT_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_ROUTING_TIERS_PATH = _PROJECT_ROOT / "src/omnimarket/configs/routing_tiers.yaml"

# The z.ai host. Any backend pointing here is subject to this module.
_ZAI_HOST = "api.z.ai"

# The ONLY surface this account's plan is served at.
CODING_PLAN_BASE = "https://api.z.ai/api/coding/paas/v4"
CODING_PLAN_PATH_PREFIX = "/api/coding/paas/v4"

# The pay-as-you-go surface. We hold no balance and no resource package on it,
# so any GLM endpoint that resolves here is a routing defect.
PAY_AS_YOU_GO_PATH_PREFIX = "/api/paas/v4"

# Model ids the Coding Plan ACCEPTED on the probe date below. Pinned together
# with the date on purpose: re-pin both or neither. An id absent from this set
# has not been shown to work on this plan, and a wrong id fails closed.
CODING_PLAN_SERVED_MODELS: frozenset[str] = frozenset(
    {
        "glm-5.3",
        "glm-5-turbo",
        "glm-5.3-flash",
        "glm-4.6",
    }
)
CODING_PLAN_PROBE_DATE = "2026-08-30"

# Cheapest-first doctrine: of the accepted ids, the flash variant is cheapest,
# so it is what the cheap_cloud rung should carry.
CHEAPEST_SERVED_MODEL = "glm-5.3-flash"

_WRONG_SURFACE_MESSAGE = (
    "GLM endpoint resolves to the z.ai PAY-AS-YOU-GO surface "
    f"({PAY_AS_YOU_GO_PATH_PREFIX}). This account holds a GLM **Coding Plan**, "
    f"which is served ONLY at {CODING_PLAN_BASE}. A Coding-Plan key sent to the "
    "pay-as-you-go surface is refused with 429 code 1113 'Insufficient balance "
    "or no resource package' — that is a WRONG-ENDPOINT signal, not a billing "
    "one. Fix the endpoint; do not fund the account."
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _zai_backends() -> list[dict]:
    """Every declared backend whose endpoint host is z.ai."""
    backends = _load(_BIFROST_PATH).get("backends") or []
    out = []
    for backend in backends:
        url = backend.get("endpoint_url")
        if isinstance(url, str) and urlparse(url).hostname == _ZAI_HOST:
            out.append(backend)
    return out


@pytest.mark.unit
def test_zai_backends_are_declared_at_all() -> None:
    """Guard the guard: an empty match set would make every test below vacuous."""
    backends = _zai_backends()
    assert backends, (
        "No z.ai backend found in the routing authority "
        f"({_BIFROST_PATH}). This module's other assertions would pass "
        "vacuously. If GLM was deliberately removed, delete this module in the "
        "same change; do not leave it green and meaningless."
    )


@pytest.mark.unit
def test_every_zai_endpoint_uses_the_coding_plan_surface() -> None:
    """No GLM endpoint may sit on the pay-as-you-go surface.

    This is the assertion that would have caught OMN-16891's repoint at review
    time instead of in a live 429 two days later.
    """
    offenders = []
    for backend in _zai_backends():
        path = urlparse(backend["endpoint_url"]).path
        if not path.startswith(CODING_PLAN_PATH_PREFIX):
            offenders.append((backend.get("backend_id"), backend["endpoint_url"]))

    assert not offenders, (
        _WRONG_SURFACE_MESSAGE
        + "\nOffending backends: "
        + ", ".join(f"{bid} -> {url}" for bid, url in offenders)
    )


@pytest.mark.unit
def test_bare_pay_as_you_go_prefix_is_rejected_by_name() -> None:
    """The rejection must NAME the Coding Plan, not just fail.

    A bare assertion failure sends the next reader back to the z.ai console to
    look at a balance. The message is the payload of this test.
    """
    for backend in _zai_backends():
        path = urlparse(backend["endpoint_url"]).path
        is_pay_as_you_go = path.startswith(
            PAY_AS_YOU_GO_PATH_PREFIX
        ) and not path.startswith(CODING_PLAN_PATH_PREFIX)
        assert not is_pay_as_you_go, (
            f"backend {backend.get('backend_id')!r}: " + _WRONG_SURFACE_MESSAGE
        )

    assert "Coding Plan" in _WRONG_SURFACE_MESSAGE
    assert CODING_PLAN_BASE in _WRONG_SURFACE_MESSAGE


@pytest.mark.unit
def test_glm_backend_model_is_in_the_plans_served_set() -> None:
    """The declared model id must be one the plan actually accepted.

    A wrong id fails closed at the provider, which looks identical to an outage
    from the ladder's side. Pinning the probed set makes a bad id a red test
    instead of a mystery 4xx.
    """
    for backend in _zai_backends():
        model = backend.get("model_name")
        assert model in CODING_PLAN_SERVED_MODELS, (
            f"backend {backend.get('backend_id')!r} declares model {model!r}, "
            "which is not in the set the GLM Coding Plan was probed to serve on "
            f"{CODING_PLAN_PROBE_DATE}: {sorted(CODING_PLAN_SERVED_MODELS)}. "
            "If z.ai's served set changed, re-probe and re-pin the set and the "
            "probe date together."
        )


@pytest.mark.unit
def test_cheap_cloud_glm_rung_carries_the_cheapest_served_model() -> None:
    """Cheapest-first: the cheap_cloud rung takes the cheapest ACCEPTED id."""
    tiers = _load(_ROUTING_TIERS_PATH)["tiers"]
    cheap_cloud = next(t for t in tiers if t["name"] == "cheap_cloud")
    glm_rungs = [m for m in cheap_cloud["models"] if m.get("backend_id") == "cloud-glm"]

    assert glm_rungs, (
        "cheap_cloud declares no cloud-glm rung. GLM is a funded, independent "
        "quota domain beside Gemini (OMN-17193); a backend no tier references "
        "is dead config."
    )
    for rung in glm_rungs:
        assert rung["id"] == CHEAPEST_SERVED_MODEL, (
            f"cheap_cloud cloud-glm rung declares id {rung['id']!r}; "
            f"cheapest-first wants {CHEAPEST_SERVED_MODEL!r}, the cheapest id "
            f"the plan accepted on {CODING_PLAN_PROBE_DATE}."
        )


@pytest.mark.unit
def test_routing_tier_glm_id_matches_the_backend_model_name() -> None:
    """Tier id and backend model_name must not drift apart.

    The ``task_model_overrides`` escape hatch in task_class_contracts.v1.yaml
    resolves by id match; a mismatch strands routing silently.
    """
    backends = {b["backend_id"]: b for b in _load(_BIFROST_PATH)["backends"]}
    tiers = _load(_ROUTING_TIERS_PATH)["tiers"]

    for tier in tiers:
        for model in tier.get("models") or []:
            backend_id = model.get("backend_id")
            backend = backends.get(backend_id)
            if backend is None or backend_id != "cloud-glm":
                continue
            assert model["id"] == backend["model_name"], (
                f"tier {tier['name']!r} declares GLM id {model['id']!r} but "
                f"backend {backend_id!r} serves {backend['model_name']!r}. "
                "Keep them in sync or the id-match override cannot resolve."
            )


@pytest.mark.unit
def test_zai_1113_is_classified_as_a_wrong_endpoint_not_a_funding_action() -> None:
    """The 1113 rule must carry the cause, not just the disposition.

    The provider's own text says 'Please recharge'. On this account that advice
    is wrong. The contract has to say so, because the provider will not.
    """
    policy = _load(_BIFROST_PATH)["provider_quota_policy"]
    zai = next(p for p in policy["providers"] if p["match_endpoint_host"] == _ZAI_HOST)
    rule = next(c for c in zai["codes"] if c["code"] == "1113")

    hint = rule.get("alert_hint") or ""
    assert hint, (
        "z.ai code 1113 carries no alert_hint. Without it the verdict reason "
        "repeats the provider's 'no balance' text and sends the next operator "
        "to the billing page for a routing defect."
    )
    assert "coding" in hint.lower(), "the hint must name the Coding Plan endpoint"
    assert "billing" in hint.lower() or "never" in hint.lower(), (
        "the hint must say this is not a billing action"
    )
