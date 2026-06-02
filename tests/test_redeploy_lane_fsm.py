# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-policy, digest-gate, and extended-FSM tests for node_redeploy (OMN-12577).

Covers the Phase 2 deltas to the live node_redeploy WorkflowPackage:

  - lane policy maps dev / stability-test / prod to compose project, overlays,
    and health targets;
  - the post-deploy verification segment (VERIFY_HEALTH -> PROBING -> SWEEPING ->
    EVIDENCE_REDUCING -> OCC_DRAFTING -> OCC_VALIDATING -> READINESS_SCORING ->
    READY | BLOCKED) is additive — the base IDLE..DONE segment is unchanged;
  - the production same-digest gate blocks a direct prod deploy without a
    matching stability-test READY digest, and blocks a digest mismatch;
  - a failed post-deploy probe triggers a rollback intent;
  - the FSM/lane logic is pure — no subprocess / Docker / deploy-agent / LLM calls
    leak out of the handlers except through handler_redeploy_kafka.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_redeploy.handlers.deployment_adapter import (
    DEFAULT_PREVIOUS_IMAGE,
    TOPIC_REDEPLOY_ROLLED_BACK,
)
from omnimarket.nodes.node_redeploy.handlers.handler_workflow_runner import (
    ModelRedeployWorkflowInput,
    run_redeploy_workflow,
)
from omnimarket.nodes.node_redeploy.models.model_lane_policy import (
    ModelStabilityReadiness,
    evaluate_prod_digest_gate,
    lane_target,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    _VERIFICATION_SEQUENCE,
    TERMINAL_PHASES,
    EnumRedeployPhase,
    next_phase,
    next_verification_phase,
)

_HANDLERS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_redeploy"
    / "handlers"
)
_DIGEST_STABILITY = "sha256:aaaa1111"
_DIGEST_PROD_DRIFT = "sha256:bbbb2222"


# ---------------------------------------------------------------------------
# Lane policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLanePolicy:
    def test_dev_target(self) -> None:
        target = lane_target(EnumRuntimeLane.DEV)
        assert target.compose_project == "omnibase-infra"
        assert target.rebuilds_from_source is True
        assert any("8085" in t for t in target.health_targets)

    def test_stability_target(self) -> None:
        target = lane_target(EnumRuntimeLane.STABILITY_TEST)
        assert target.compose_project == "omnibase-infra-stability-test"
        assert "docker-compose.stability-test.yml" in target.compose_files
        assert any("18085" in t for t in target.health_targets)

    def test_prod_target_never_rebuilds(self) -> None:
        target = lane_target(EnumRuntimeLane.PROD)
        assert target.compose_project == "omnibase-infra-prod"
        assert "docker-compose.prod.yml" in target.compose_files
        assert any("28085" in t for t in target.health_targets)
        # Production must never rebuild — it deploys a stability-proven digest.
        assert target.rebuilds_from_source is False

    def test_all_lanes_have_distinct_projects(self) -> None:
        projects = {lane_target(lane).compose_project for lane in EnumRuntimeLane}
        assert len(projects) == len(list(EnumRuntimeLane))


# ---------------------------------------------------------------------------
# Production same-digest gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProdDigestGate:
    def test_prod_without_digest_blocked(self) -> None:
        decision = evaluate_prod_digest_gate(
            requested_digest=None, stability_readiness=None
        )
        assert decision.allowed is False
        assert "image_digest" in decision.reason

    def test_prod_without_stability_readiness_blocked(self) -> None:
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=None
        )
        assert decision.allowed is False
        assert "stability" in decision.reason.lower()

    def test_prod_with_failed_stability_blocked(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=False)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=readiness
        )
        assert decision.allowed is False
        assert "readiness failed" in decision.reason.lower()

    def test_prod_digest_mismatch_blocked(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=True)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_PROD_DRIFT, stability_readiness=readiness
        )
        assert decision.allowed is False
        assert "does not match" in decision.reason

    def test_prod_matching_stability_allowed_reuses_digest(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=True)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=readiness
        )
        assert decision.allowed is True
        # prod reuses the exact stability digest — no rebuild.
        assert decision.image_digest == _DIGEST_STABILITY


# ---------------------------------------------------------------------------
# Extended FSM transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtendedFsm:
    def test_base_segment_unchanged(self) -> None:
        # IDLE -> SYNC_CLONES -> ... -> VERIFY_HEALTH -> DONE must be intact.
        phase = next_phase(EnumRedeployPhase.IDLE)
        assert phase == EnumRedeployPhase.SYNC_CLONES
        chain = [EnumRedeployPhase.IDLE]
        cur = EnumRedeployPhase.IDLE
        while cur != EnumRedeployPhase.DONE:
            cur = next_phase(cur)
            chain.append(cur)
        assert chain == [
            EnumRedeployPhase.IDLE,
            EnumRedeployPhase.SYNC_CLONES,
            EnumRedeployPhase.UPDATE_PINS,
            EnumRedeployPhase.REBUILD,
            EnumRedeployPhase.SEED_INFISICAL,
            EnumRedeployPhase.VERIFY_HEALTH,
            EnumRedeployPhase.DONE,
        ]

    def test_verification_segment_legal_order(self) -> None:
        chain = [EnumRedeployPhase.VERIFY_HEALTH]
        cur = EnumRedeployPhase.VERIFY_HEALTH
        while cur != EnumRedeployPhase.READINESS_SCORING:
            cur = next_verification_phase(cur)
            chain.append(cur)
        assert chain == [
            EnumRedeployPhase.VERIFY_HEALTH,
            EnumRedeployPhase.PROBING,
            EnumRedeployPhase.SWEEPING,
            EnumRedeployPhase.EVIDENCE_REDUCING,
            EnumRedeployPhase.OCC_DRAFTING,
            EnumRedeployPhase.OCC_VALIDATING,
            EnumRedeployPhase.READINESS_SCORING,
        ]

    def test_readiness_scoring_is_gate_decided(self) -> None:
        # READINESS_SCORING does not auto-advance; READY/BLOCKED come from the gate.
        with pytest.raises(ValueError, match="READY or BLOCKED"):
            next_verification_phase(EnumRedeployPhase.READINESS_SCORING)

    def test_ready_and_blocked_and_rolled_back_are_terminal(self) -> None:
        assert EnumRedeployPhase.READY in TERMINAL_PHASES
        assert EnumRedeployPhase.BLOCKED in TERMINAL_PHASES
        assert EnumRedeployPhase.ROLLED_BACK in TERMINAL_PHASES

    def test_verification_phases_not_in_base_sequence(self) -> None:
        # Additive: the verification phases must not appear in the legacy base
        # next_phase() walk (golden-chain shape stays byte-identical).
        cur = EnumRedeployPhase.IDLE
        base_walk: set[EnumRedeployPhase] = {cur}
        while cur != EnumRedeployPhase.DONE:
            cur = next_phase(cur)
            base_walk.add(cur)
        assert base_walk.isdisjoint(set(_VERIFICATION_SEQUENCE))


# ---------------------------------------------------------------------------
# Workflow-level lane gate + rollback intent
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWorkflowLaneGate:
    async def test_direct_prod_deploy_without_stability_is_blocked(self) -> None:
        # A prod deploy with a pinned digest but no stability readiness threaded
        # through must be BLOCKED before any deploy effect — no event bus needed.
        from uuid import uuid4

        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_STABILITY,
            promotion_batch_id="batch-1",
            dry_run=False,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)

        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.BLOCKED
        assert result.rebuild_result is None

    async def test_failed_probe_triggers_rollback_intent(self) -> None:
        # A dev deploy that succeeds but fails the smoke probe rolls back and
        # publishes the rolled-back event with the restored image.
        import json
        from uuid import uuid4

        from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
            EnumRedeployStatus,
            ModelDeployPhaseResults,
            ModelDeployRebuildCompleted,
        )

        bus = EventBusInmemory(environment="test", group="lane-fsm-test")
        await bus.start()
        corr_id = uuid4()
        rolled_back: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rolled_back.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_REDEPLOY_ROLLED_BACK,
            on_message=_on_rollback,
            group_id="rollback-capture",
        )

        async def _agent_success(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completed = ModelDeployRebuildCompleted(
                correlation_id=payload["correlation_id"],
                status=EnumRedeployStatus.SUCCESS,
                duration_seconds=5.0,
                git_sha="sha",
                services_restarted=["omninode-runtime"],
                phase_results=ModelDeployPhaseResults(),
                errors=[],
            )
            await bus.publish(
                "onex.evt.deploy.rebuild-completed.v1",
                key=payload["correlation_id"].encode(),
                value=json.dumps(completed.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            "onex.cmd.deploy.rebuild-requested.v1",
            on_message=_agent_success,
            group_id="fake-agent",
        )

        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=corr_id,
            runtime_lane=EnumRuntimeLane.DEV,
            dry_run=False,
            smoke_test=True,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=bus)

        assert result.rolled_back is True
        assert result.success is False
        assert len(rolled_back) == 1
        assert rolled_back[0]["restored_image"] == DEFAULT_PREVIOUS_IMAGE

        await bus.close()


# ---------------------------------------------------------------------------
# Effect boundary: handlers must not call subprocess / Docker / LLM directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEffectBoundary:
    _FORBIDDEN_MODULES = {"subprocess", "docker"}
    # handler_redeploy_kafka is the only deploy actuator and is allowed to import
    # the event bus; the FSM / runner / adapter logic must not shell out.
    _GUARDED_FILES = (
        "handler_redeploy.py",
        "handler_workflow_runner.py",
        "deployment_adapter.py",
    )

    def test_guarded_handlers_have_no_subprocess_or_docker(self) -> None:
        for name in self._GUARDED_FILES:
            source = (_HANDLERS_DIR / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            leaked = imported & self._FORBIDDEN_MODULES
            assert not leaked, f"{name} must not import {leaked}"

    def test_only_kafka_handler_owns_deploy_topics(self) -> None:
        # The deploy-agent rebuild topics live only in handler_redeploy_kafka;
        # the FSM and runner must not hardcode them (no second actuator).
        for name in ("handler_redeploy.py", "handler_workflow_runner.py"):
            source = (_HANDLERS_DIR / name).read_text(encoding="utf-8")
            assert "rebuild-requested" not in source, (
                f"{name} must reach the deploy agent only via handler_redeploy_kafka"
            )
