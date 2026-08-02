# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15638 — the OpenRouter model slug is a two-surface seam; match it.

Background (the incident this file exists for): the ``cheap_frontier`` rung
pinned ``qwen/qwen3-coder:free``. OpenRouter RETIRED that slug to paid-only
without notice. Live, through the configured endpoint and the configured key::

    POST https://openrouter.ai/api/v1/chat/completions  model=qwen/qwen3-coder:free
      HTTP 404
      {"error":{"message":"This model is unavailable for free. The paid version
       is available now - use this slug instead: qwen/qwen3-coder","code":404}}

The slug had also vanished from ``GET /api/v1/models`` entirely. It was not a
credential defect — the key resolved (len 73).

**The fragility lesson: a free slug is not a durable pin.** Providers yank
``:free`` variants to paid with no deprecation window, so the pin rots in place
while the config still claims zero cost. The *liveness* detector for that is the
ACTIVATE C3 probe, which drives the real provider; this module cannot and does
not duplicate it — a network call is not a unit test, and an opt-in network test
that skips in CI is exactly the advisory surface CLAUDE.md rule 5 rejects.

What this module DOES close is the failure mode that made the repair risky: the
same logical backend pins its slug in **two** committed surfaces, and repairing
one silently leaves the other lying.

  * ``src/omnimarket/configs/bifrost_delegation.yaml``  — the slug actually POSTed
  * ``src/omnimarket/data/model_registry/model_registry_v1.yaml`` — the slug the
    cost/routing registry claims for the same ``backend_id``/``model_id``

A partial repair leaves the registry advertising a dead model and the platform
reasoning about pricing and context windows for a model it never calls. Per
``feedback_define_and_match_seams`` that is a seam, and seams get a test that
drives both sides — not two independent suites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIFROST_PATH = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
)
_REGISTRY_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)

# The one backend whose slug is pinned on BOTH surfaces under the same key.
# Keyed by the shared identifier so the assertion cannot drift onto a different
# backend if either file is reordered.
_SHARED_KEY = "openrouter-qwen3-coder-480b"


def _bifrost_backends() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(_BIFROST_PATH.read_text(encoding="utf-8"))
    return {b["backend_id"]: b for b in data["backends"]}


def _registry_models() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))
    models: dict[str, dict[str, Any]] = data["models"]
    return models


@pytest.mark.unit
def test_bifrost_and_registry_pin_the_same_openrouter_slug() -> None:
    """Both committed surfaces must name the SAME provider slug.

    RED before OMN-15638's registry half: bifrost said
    ``nvidia/nemotron-3-ultra-550b-a55b:free`` while the registry still said the
    retired ``qwen/qwen3-coder:free``.
    """
    backend = _bifrost_backends()[_SHARED_KEY]
    model = _registry_models()[_SHARED_KEY]

    assert backend["model_name"] == model["model_name"], (
        f"slug seam mismatch for {_SHARED_KEY!r}: bifrost POSTs "
        f"{backend['model_name']!r} but the model registry declares "
        f"{model['model_name']!r}. Repair BOTH surfaces or neither."
    )


@pytest.mark.unit
def test_cheap_frontier_zero_cost_declaration_matches_a_free_slug() -> None:
    """A zero-cost declaration is only honest over a ``:free`` slug.

    This is the mechanism behind the never-silent-paid rule (OMN-14224 /
    OMN-14225), not a promise in a PR body: if someone repoints this rung at a
    metered slug — e.g. the ``qwen/qwen3-coder`` the provider's own 404 body
    recommends, at $0.30/$1.00 per M — while the registry still declares
    ``0.00``/``zero_marginal_api_cost``, this reddens.

    Adopting a paid slug is a legitimate operator decision; doing it while the
    config keeps claiming free is not. The fix on that path is to update the
    pricing here (and ``routing_tiers.yaml``'s ``cost_type``) in the same
    change, which turns this test green again on truthful values.
    """
    model = _registry_models()[_SHARED_KEY]
    declares_free = (
        model["pricing_per_1m_input"] == "0.00"
        and model["pricing_per_1m_output"] == "0.00"
        and model["cost_basis"] == "zero_marginal_api_cost"
    )
    if not declares_free:
        pytest.skip(
            "registry no longer declares this rung free — the paid path is "
            "explicitly priced, which is the loud version this test allows"
        )

    assert model["model_name"].endswith(":free"), (
        f"{_SHARED_KEY!r} declares zero marginal cost but pins the metered slug "
        f"{model['model_name']!r} — that is the silent-paid path OMN-14224 / "
        "OMN-14225 closed. Either pin a :free slug or price it truthfully."
    )


@pytest.mark.unit
def test_cheap_frontier_does_not_pin_a_known_retired_slug() -> None:
    """Ratchet: slugs proven dead against the live provider stay out of this tier.

    Live-probed 2026-08-01 through the configured endpoint + key — recorded
    observations with the provider's verbatim refusal, not guesses. A ratchet is
    not a catalog: it cannot detect the NEXT retirement (only the C3 liveness
    probe drives the real provider), but it does stop a revert or a copy-paste
    from resurrecting one that already burned us.

    Scoped to ``cheap_frontier`` because that is the tier this repair owns. The
    same probe found ``thudm/glm-4-9b-chat:free`` — pinned by the
    ``openrouter-glm-flash`` backend on the ``cheap_cloud`` tier — is ALSO dead
    (HTTP 400, "thudm/glm-4-9b-chat:free is not a valid model ID"). That is a
    different tier with different declared economics, so repointing it is a
    separate routing decision, reported on OMN-15638 rather than made here.
    Widening this assertion to every OpenRouter backend is the right follow-up
    once that decision lands — do it by deleting the tier filter, not by adding
    an allowlist entry.
    """
    retired: dict[str, str] = {
        # HTTP 404: "This model is unavailable for free. The paid version is
        # available now - use this slug instead: qwen/qwen3-coder"
        "qwen/qwen3-coder:free": "OMN-15638 — retired to paid-only",
        # HTTP 400: "thudm/glm-4-9b-chat:free is not a valid model ID"
        "thudm/glm-4-9b-chat:free": "OMN-15638 — invalid model ID",
    }

    offenders = [
        f"{backend_id} pins {b['model_name']!r} ({retired[b['model_name']]})"
        for backend_id, b in _bifrost_backends().items()
        if b.get("tier") == "cheap_frontier" and b.get("model_name") in retired
    ]
    assert not offenders, "retired OpenRouter slug(s) re-pinned: " + "; ".join(
        offenders
    )
