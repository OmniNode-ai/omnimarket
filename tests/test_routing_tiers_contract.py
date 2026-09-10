# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for routing tier contract model ids."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.validators.routing_tier_backend_bindability import (
    LANE_BOUND_LOCAL_BACKENDS,
    declared_backends,
    find_unbindable_tier_backends,
    format_findings,
)
from tests.constants import MODEL_QWEN3_27B_MTP, MODEL_QWEN3_35B_A3B

_ROUTING_TIERS_PATH = Path("src/omnimarket/configs/routing_tiers.yaml")

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
    """OMN-16833 (re-expresses OMN-16442's re-expression of OMN-15180).

    OMN-15180 registered a second local ``code_generation`` backend so the
    OMN-14402 same-tier fallback (``sibling_backend_available_in_tier``) had a
    sibling to retry before escalating to the metered cheap_cloud tier.
    OMN-16442 turned that membership pin into the property: >= 2 DISTINCT local
    code_generation backends.

    That property is currently NOT deliverable by the fleet, and asserting it
    over DECLARED entries was measuring the config rather than the fleet. The
    second declarant was ``local-ds-v4-flash`` at .200:8101, which every lane
    overlay marks ``serving: false`` (OMN-16999) and every lane therefore
    renders as ``endpoint_url: null`` — so ``_load_bifrost_endpoints`` dropped
    it and the retry sibling did not exist in fact. The old assertion was green
    on a rung the whole fleet skips, which is the exact defect OMN-16833 is
    about; keeping it green by re-declaring an unbindable entry would be
    circular.

    Asserted here in two halves, so the guarantee is honest today and restores
    itself the moment the endpoint comes back:

    1. UNCONDITIONAL — code_generation keeps at least one local rung, and every
       local rung a tier references is one a lane actually binds. This is what
       stops the class falling straight to the metered tier.
    2. CONDITIONAL — every lane-bound local backend whose contract declares the
       ``code_generation`` capability must be referenced by the local tier. So
       when ds-v4-flash is restarted and moves into
       ``LANE_BOUND_LOCAL_BACKENDS``, this fails until its tier entry is
       restored, and OMN-15180's >= 2 sibling guarantee returns with it. The
       ratchet is deferred, not dropped.
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

    assert code_gen_backends, (
        "code_generation lost its last LOCAL rung — it would fall straight to "
        "the metered cheap_cloud tier on every request"
    )
    unbindable = code_gen_backends - LANE_BOUND_LOCAL_BACKENDS
    assert not unbindable, (
        "the local tier references code_generation backend(s) no lane binds "
        f"with serving: true: {sorted(unbindable)} — a rung declared and "
        "unreachable at the same time"
    )
    assert not (code_gen_backends & _RETIRED_BACKEND_REFS)

    declarations = declared_backends()
    capable_and_bound = {
        backend_id
        for backend_id in LANE_BOUND_LOCAL_BACKENDS
        if "code_generation" in (declarations[backend_id].get("capabilities") or ())
    }
    unreferenced = capable_and_bound - code_gen_backends
    assert not unreferenced, (
        "these local backends are bound by a lane AND declare the "
        f"code_generation capability, but no tier references them: "
        f"{sorted(unreferenced)}. Restore the tier entry — OMN-15180's "
        "same-tier retry sibling depends on it."
    )


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
    """OMN-16833 AC1: a referenced backend must be bindable somewhere.

    ``_load_bifrost_endpoints`` drops any backend without a complete
    ``endpoint_url``, SILENTLY. So a tier can point at a backend that resolves
    to nothing on every lane in the fleet and no surface anywhere says so — the
    rung is declared and unreachable at the same time, which is exactly the
    decorative-rung class the OMN-15630 routing-completeness gate exists to
    forbid. The observable effect is the one this ticket was filed for: the
    cheapest-first ladder degrades to paid-first for the classes that rung was
    the local answer for.

    The classification rules and their live probe evidence live in
    ``omnimarket.validators.routing_tier_backend_bindability`` so the pre-commit
    hook, the CI job and this test all decide from one implementation. This
    failed on the pre-change tree naming ``cloud-vertex-gemini``
    (cloud_endpoint_url_null) and ``local-ds-v4-flash`` (local_bound_by_no_lane).
    """
    findings = find_unbindable_tier_backends()

    assert not findings, format_findings(findings)


def test_bindability_validator_catches_a_parked_backend_that_is_referenced() -> None:
    """The gate must FAIL on the shape it exists to reject, not merely pass today.

    A validator that only ever runs against a clean tree cannot distinguish
    "no offenders" from "the check does not work" — the positive control this
    repo requires for any asserted zero. Re-reference the parked
    ``cloud-vertex-gemini`` in a scratch copy of the tiers file and require the
    validator to name it.
    """
    tiers = yaml.safe_load(_ROUTING_TIERS_PATH.read_text(encoding="utf-8"))
    cheap_cloud = next(tier for tier in tiers["tiers"] if tier["name"] == "cheap_cloud")
    cheap_cloud["models"].append(
        {
            "id": "vertex-gemini-flash",
            "backend_id": "cloud-vertex-gemini",
            "max_context_tokens": 1000000,
            "use_for": ["summarization"],
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "routing_tiers.yaml"
        scratch.write_text(yaml.safe_dump(tiers), encoding="utf-8")
        findings = find_unbindable_tier_backends(tiers_path=scratch)

    assert findings.get("cloud_endpoint_url_null") == ["cloud-vertex-gemini"], findings
