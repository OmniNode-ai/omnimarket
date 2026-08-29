# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for routing tier contract model ids."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from tests.constants import MODEL_QWEN3_27B_MTP, MODEL_QWEN3_35B_A3B

_ROUTING_TIERS_PATH = Path("src/omnimarket/configs/routing_tiers.yaml")
_BIFROST_CONTRACT_PATH = Path("src/omnimarket/configs/bifrost_delegation.yaml")
# The one bifrost ``tier`` value the typed lane overlay is able to bind.
_LANE_BOUND_TIER = "local"

# OMN-16442: backends whose ENDPOINT no longer exists on the fleet. Both were
# re-probed 2026-08-28 against the canonical inventory
# (omni_home/docs/reference/AI_LAB_HARDWARE.md, "Last verified: 2026-08-28"):
#   local-reasoner   -> .201:8001, the RTX 4090 slot physically removed for RMA
#                       (OMN-16407); curl exit 7 "Couldn't connect to server".
#   local-coder-mlx  -> .200:8401, gone; the Mac Studio's MLX server now serves
#                       Qwen3.8-27B-8bit on 127.0.0.1:8099, LOCALHOST-ONLY and
#                       therefore not reachable from the .201 runtime.
_RETIRED_BACKEND_REFS: frozenset[str] = frozenset({"local-reasoner", "local-coder-mlx"})
_RETIRED_MODEL_IDS: frozenset[str] = frozenset(
    {MODEL_QWEN3_27B_MTP, "mlx-community/Qwen3.6-35B-A3B-8bit"}
)


def _load_config() -> object:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text(encoding="utf-8"))


def test_routing_tiers_declares_no_retired_local_backends() -> None:
    """OMN-16442 (supersedes OMN-12709's local-reasoner pin).

    OMN-12709 asserted that ``Qwen3.6-27B-MTP-IQ4_XS.gguf`` resolves to the
    ``local-reasoner`` backend at 24576 tokens. That backend's endpoint
    (.201:8001) is the RTX 4090 slot physically removed for RMA, so the
    assertion pinned the contract to a model that cannot answer. It is
    inverted here: the retired backends must be ABSENT, so a future edit
    cannot quietly reintroduce a rung that resolves to nothing.
    """
    config = _load_config()

    declared_refs = {
        model.backend_ref for tier in config.tiers for model in tier.models
    }
    declared_ids = {model.id for tier in config.tiers for model in tier.models}

    assert not (declared_refs & _RETIRED_BACKEND_REFS), (
        "routing_tiers.yaml declares a retired backend whose endpoint is dead: "
        f"{sorted(declared_refs & _RETIRED_BACKEND_REFS)}. Register the "
        "REPLACEMENT hardware instead of reviving these backend_ids."
    )
    assert not (declared_ids & _RETIRED_MODEL_IDS), (
        "routing_tiers.yaml declares a model id served nowhere on the fleet: "
        f"{sorted(declared_ids & _RETIRED_MODEL_IDS)}"
    )


def test_routing_tiers_declares_live_local_served_model_ids() -> None:
    """OMN-12721: local routing ids must match the live .201 provider ids.

    OMN-16442: the ``MODEL_QWEN3_27B_MTP`` membership assertion was REMOVED —
    that id is the retired local-reasoner artifact (see the test above). The
    surviving positive assertion is the live SGLang served id at .201:8000,
    re-probed 2026-08-28: GET /v1/models -> id "qwen3.8", max_model_len 122880.
    """
    config = _load_config()

    by_id = {model.id: model for tier in config.tiers for model in tier.models}

    assert MODEL_QWEN3_35B_A3B in by_id
    assert "qwen3-coder-30b" not in by_id
    assert "deepseek-r1-14b" not in by_id


def test_local_tier_keeps_a_same_tier_sibling_for_code_generation() -> None:
    """OMN-16442 (supersedes OMN-15180's local-coder-mlx membership pin).

    OMN-15180 registered ``local-coder-mlx`` in the local tier so the OMN-14402
    same-tier fallback (``sibling_backend_available_in_tier``) had a sibling to
    retry before escalating to the metered cheap_cloud tier. That backend's
    endpoint (.200:8401) is dead, so the membership itself is retired — but the
    PROPERTY it was protecting is not. Assert the property directly: the local
    tier must still declare at least TWO backends serving ``code_generation``,
    otherwise a single transport failure escalates the whole tier to cloud.
    """
    config = _load_config()

    local_tier = next(tier for tier in config.tiers if tier.name == "local")
    # DISTINCT backends, not model entries: two entries pointing at the same
    # backend_ref give `sibling_backend_available_in_tier` nothing to retry
    # after a transport failure, so counting entries would pass while the
    # property this test exists to protect is violated (CodeRabbit, OMN-16442).
    code_gen_backends = {
        model.backend_ref
        for model in local_tier.models
        if "code_generation" in model.use_for
    }

    assert len(code_gen_backends) >= 2, (
        "the local tier must keep >=2 DISTINCT code_generation backends so "
        "OMN-14402's same-tier fallback has a sibling to retry before "
        f"escalating to the metered cheap_cloud tier; got {sorted(code_gen_backends)}"
    )
    assert not (code_gen_backends & _RETIRED_BACKEND_REFS)


def test_prose_classes_kept_a_local_rung_after_reasoner_retirement() -> None:
    """OMN-16442: ``test``, ``documentation`` and ``summarization`` were served
    in the local tier ONLY by the retired ``local-reasoner``. Deleting that rung
    without rehoming them would have demoted all three to the metered
    cheap_cloud tier.

    All three declare ``local`` in their ``escalation_policy.tier_order``, so a
    local declarant is not merely nice to have — the OMN-15630
    routing-completeness gate rejects a declared tier that serves none of a
    class's capabilities.
    """
    config = _load_config()

    local_tier = next(tier for tier in config.tiers if tier.name == "local")

    for task_type in ("test", "documentation", "summarization"):
        declarants = {
            model.backend_ref
            for model in local_tier.models
            if task_type in model.use_for
        }
        assert declarants, (
            f"task_type {task_type!r} lost its last LOCAL backend when "
            "local-reasoner was retired — it would fall straight to the "
            "metered cheap_cloud tier"
        )


def test_no_tier_references_a_backend_no_lane_can_bind() -> None:
    """OMN-16833 AC1/AC3: a referenced backend must be bindable somewhere.

    ``_load_bifrost_endpoints`` drops any backend without a complete
    ``endpoint_url``, SILENTLY. So a tier can reference a backend that no lane
    is able to resolve and nothing anywhere says so — the rung is declared and
    unreachable at the same time, which is exactly the decorative-rung class the
    OMN-15630 routing-completeness gate exists to forbid.

    Two binding paths exist, and only two:

    * ``tier: local`` backends are legitimately ``endpoint_url: null`` in the
      committed contract. They are bound per lane by the typed overlay
      (``omnibase_infra`` ``docker/lane-overlays/<lane>.bifrost.yaml``, validated
      by ``ModelBifrostLaneBackendBinding``), which admits ONLY unauthenticated
      local ``http://`` chat-completions endpoints on an authorized host table.
    * every other backend must therefore carry a concrete ``endpoint_url`` in
      the committed contract, because the lane overlay cannot represent it and
      ``render_bifrost_delegation_contract`` "never reads endpoint or model
      bindings from the process environment" (its own module docstring).

    A non-local backend referenced by a tier with ``endpoint_url: null`` is
    unbindable by construction on every lane. Live readback 2026-08-29 on the
    dev lane (``docker exec omninode-runtime``) confirmed the consequence:
    ``cloud-vertex-gemini`` was declared, referenced by ``cheap_cloud``, and
    absent from ``BACKENDS_LOADED``.

    Parking such a backend is fine — leaving a tier POINTING at it is not.
    """
    config = _load_config()

    declarations = {
        entry["backend_id"]: entry
        for entry in yaml.safe_load(_BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))[
            "backends"
        ]
    }
    referenced = {model.backend_ref for tier in config.tiers for model in tier.models}

    undeclared = sorted(referenced - set(declarations))
    assert not undeclared, (
        "routing_tiers.yaml references backend_ids that bifrost_delegation.yaml "
        f"does not declare at all: {undeclared}"
    )

    unbindable = sorted(
        backend_ref
        for backend_ref in referenced
        if declarations[backend_ref].get("tier") != _LANE_BOUND_TIER
        and not declarations[backend_ref].get("endpoint_url")
    )
    assert not unbindable, (
        "routing_tiers.yaml references non-local backends that carry "
        f"endpoint_url: null and so no lane can bind: {unbindable}. "
        "_load_bifrost_endpoints drops them silently, leaving a tier pointing "
        "at nothing. Either give the backend a concrete endpoint_url, or stop "
        "referencing it from every tier and leave the declaration parked."
    )
