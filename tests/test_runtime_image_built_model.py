# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-source tests for ModelRuntimeImageBuilt + canonical redeploy bus topics (OMN-13655).

Three assertions without live infra:
  (a) ModelRuntimeImageBuilt round-trips through model_validate / model_dump.
  (b) RUNTIME_IMAGE_BUILT_TOPIC_V1 is declared in
      node_redeploy_orchestrator/contract.yaml subscribe_topics.
  (c) evaluate_prod_promotion_gate is importable; a dev-lane
      ModelProdPromotionGateCommand evaluates ALLOWED unconditionally.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelRuntimeImageBuilt,
    evaluate_prod_promotion_gate,  # noqa: F401  (import proof for plan item c)
)
from omnimarket.events.topics import RUNTIME_IMAGE_BUILT_TOPIC_V1
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    evaluate_gate,
)

_CONTRACT_YAML = (
    Path(__file__).parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_redeploy_orchestrator"
    / "contract.yaml"
)

_DIGEST = "sha256:abc123def456" + "0" * 48
_SOURCE_SHA = "a" * 40
_PROVENANCE = "ci:build-runtime.yml:run_id=12345"


@pytest.mark.unit
class TestModelRuntimeImageBuilt:
    """(a) ModelRuntimeImageBuilt round-trip validation."""

    def test_round_trip_release_clean_main(self) -> None:
        """RELEASE + CLEAN_MAIN image round-trips cleanly."""
        original = ModelRuntimeImageBuilt(
            correlation_id=uuid4(),
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.RELEASE,
            promotion_class=EnumPromotionClass.CLEAN_MAIN,
            provenance=_PROVENANCE,
            runtime_lane=EnumRuntimeLane.DEV,
        )
        dumped = original.model_dump()
        restored = ModelRuntimeImageBuilt.model_validate(dumped)
        assert restored == original

    def test_round_trip_json(self) -> None:
        """model_dump(mode='json') → model_validate_json round-trip."""
        original = ModelRuntimeImageBuilt(
            correlation_id=uuid4(),
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.RELEASE,
            promotion_class=EnumPromotionClass.CLEAN_MAIN,
            provenance=_PROVENANCE,
        )
        as_json = original.model_dump_json()
        restored = ModelRuntimeImageBuilt.model_validate(json.loads(as_json))
        assert restored == original

    def test_stability_candidate_class_accepted(self) -> None:
        """STABILITY_CANDIDATE promotion_class is a valid field value."""
        model = ModelRuntimeImageBuilt(
            correlation_id=uuid4(),
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.WORKSPACE,
            promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
            provenance=_PROVENANCE,
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        )
        assert model.promotion_class is EnumPromotionClass.STABILITY_CANDIDATE

    def test_extra_fields_forbidden(self) -> None:
        """extra=forbid: unknown fields raise ValidationError."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            ModelRuntimeImageBuilt.model_validate(
                {
                    "correlation_id": str(uuid4()),
                    "digest": _DIGEST,
                    "source_sha": _SOURCE_SHA,
                    "build_source": "RELEASE",
                    "promotion_class": "CLEAN_MAIN",
                    "provenance": _PROVENANCE,
                    "unknown_extra_field": "bad",
                }
            )

    def test_frozen_model_rejects_mutation(self) -> None:
        """frozen=True: attribute assignment raises TypeError."""
        model = ModelRuntimeImageBuilt(
            correlation_id=uuid4(),
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.RELEASE,
            promotion_class=EnumPromotionClass.CLEAN_MAIN,
            provenance=_PROVENANCE,
        )
        with pytest.raises((TypeError, Exception)):
            model.digest = "sha256:mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestRuntimeImageBuiltTopicInContract:
    """(b) Contract-source proof: topic is declared in orchestrator subscribe_topics."""

    def test_runtime_image_built_topic_in_contract_subscribe_topics(self) -> None:
        """RUNTIME_IMAGE_BUILT_TOPIC_V1 must appear in node_redeploy_orchestrator subscribe_topics."""
        assert _CONTRACT_YAML.exists(), f"contract.yaml not found: {_CONTRACT_YAML}"
        contract = yaml.safe_load(_CONTRACT_YAML.read_text())
        subscribe_topics: list[str] = (
            contract.get("event_bus", {}).get("subscribe_topics", []) or []
        )
        assert RUNTIME_IMAGE_BUILT_TOPIC_V1 in subscribe_topics, (
            f"{RUNTIME_IMAGE_BUILT_TOPIC_V1!r} not found in contract subscribe_topics; "
            f"got: {subscribe_topics}"
        )


@pytest.mark.unit
class TestDevLaneGateAllowedUnconditionally:
    """(c) Dev-lane ModelProdPromotionGateCommand evaluates ALLOWED unconditionally."""

    def test_dev_lane_allowed_no_projection(self) -> None:
        """Dev lane is not gated — no projection, grant, or readiness required."""
        command = ModelProdPromotionGateCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
        )
        decision = evaluate_gate(command)
        assert decision.allowed is True, f"Expected allowed=True; got: {decision}"
        assert "dev" in decision.reason.lower()

    def test_dev_lane_allowed_with_candidate_promotion_class(self) -> None:
        """Dev lane allows STABILITY_CANDIDATE images unconditionally (no prod gate)."""
        command = ModelProdPromotionGateCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
        )
        decision = evaluate_gate(command)
        assert decision.allowed is True
