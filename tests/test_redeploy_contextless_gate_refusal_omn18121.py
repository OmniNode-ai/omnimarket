# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A gate decision with no deploy context must refuse, not fabricate one (OMN-18121).

WHAT THIS COST, MEASURED
------------------------

Five rebuild commands reached the .201 dev deploy agent between
2026-09-10T00:48:53Z and 03:31:48Z -- jobs ``d62ec83e``, ``68295ceb``,
``1316f524``, ``77343308``, ``ced0ac22``. Every one of them carried the same
payload::

    {"scope": "full", "git_ref": "origin/main", "runtime_lane": "dev",
     "build_source": "release", "requested_by": "node_redeploy_orchestrator",
     "image_ref": null, "image_digest": null, "services": []}

Not one of those values was requested by anybody. Every one is the FIELD
DEFAULT of :class:`ModelRedeployStartCommand`, filled in by the final ``else``
branch of ``_coerce_gate_result`` when a gate-evaluated event arrived carrying
neither an echoed ``start`` nor a ``deploy_context``. The agent reset the shared
deploy-source clone to ``origin/main`` on each one, 77 commits behind ``dev``,
and every CI-triggered dev rebuild died until the clone was reconciled by hand.

The correlation ids prove the ref was DISCARDED rather than merely absent: they
resolve to real start commands on ``onex.cmd.omnimarket.redeploy-start.v1``
that each declared an explicit sha and ``build_source: workspace`` --
``1316f524`` = ``gha/omnibase_infra/pr-3238`` ref ``4cff0618ae1a6f``,
``77343308`` = pr-3242 ref ``6c3acf69086868``, ``ced0ac22`` = pr-3243 ref
``46207e2a1c48cc``. The trigger was a backlog of stale gate-evaluated events
dated 2026-09-06 (offsets 139-152) whose payload is the bare decision:
``keys = [allowed, image_digest, reason, rollback_target]``.

WHY A DEFAULT IS THE WRONG SHAPE HERE
-------------------------------------

``ModelRedeployDeployContext``'s own docstring already describes this failure --
"the orchestrator then rebuilt a DEFAULTED start and would have asked the agent
to rebuild ``origin/main`` from a ``release`` artifact, whatever ref the
post-merge trigger actually published". OMN-16939 added the context so the real
values ride across the gate hop. It closed the case where the context IS
carried. It left the branch where the context is ABSENT still fabricating, and
that is the branch that fired.

A deploy request that cannot say what to deploy has no safe default: rule 8,
fail-fast on missing input, never a silent fallback. The one shape that legitimately
carries no ref is a digest-only promotion, and that one identifies its artifact
by ``image_digest`` -- so the invariant is "name an artifact", not "name a ref".
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumRedeployScope,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelRedeployCommand,
    ModelRedeployDeployContext,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers import (
    handler_redeploy_orchestrator as module,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)


class TestNoSilentRefDefault:
    """The literal is gone from the models it was defaulted into (AC1)."""

    def test_start_command_has_no_origin_main_default(self) -> None:
        start = ModelRedeployStartCommand(correlation_id=uuid4())
        assert start.git_ref != "origin/main"
        assert start.git_ref is None

    def test_deploy_context_has_no_origin_main_default(self) -> None:
        context = ModelRedeployDeployContext()
        assert context.git_ref != "origin/main"
        assert context.git_ref is None

    def test_the_literal_is_never_assigned_in_the_orchestrator_sources(self) -> None:
        """AC6: the branch literal survives only as prose, never as a value.

        Checked as an ASSIGNMENT rather than a substring. The comments and
        docstrings that explain why the literal was removed are the record of
        this incident and should stay; what must not come back is a field
        default or an argument that puts the string on a deploy request.
        """
        import re
        from pathlib import Path

        assigned = re.compile(r"""(=|default=)\s*["']origin/main["']""")
        node_root = Path(module.__file__).resolve().parents[1]
        offenders = sorted(
            str(path.relative_to(node_root))
            for path in node_root.rglob("*.py")
            if assigned.search(path.read_text())
        )
        assert offenders == [], f"origin/main assigned in: {offenders}"


class TestContextlessGateDecisionRefuses:
    """AC2: no deploy is synthesised from defaults."""

    def _bare_decision(self, *, image_digest: str | None = None) -> dict[str, object]:
        """The exact payload shape read off the topic: no deploy_context key."""
        return {
            "allowed": True,
            "image_digest": image_digest,
            "reason": "non-prod lane allows trivially",
            "rollback_target": "omninode-runtime:v2.3.1",
        }

    def test_refuses_a_bare_decision_carrying_no_artifact(self) -> None:
        correlation_id = uuid4()
        with pytest.raises(module.RedeployContextMissingError) as excinfo:
            module._coerce_gate_result(self._bare_decision(), correlation_id)

        message = str(excinfo.value)
        assert str(correlation_id) in message
        assert "deploy_context" in message
        assert "start" in message

    def test_refusal_emits_no_deploy_publish_event(self) -> None:
        """The whole point: nothing reaches the deploy agent (AC2, AC5)."""
        handler = module.HandlerRedeployOrchestrator()
        envelope = _gate_evaluated_envelope(self._bare_decision())

        with pytest.raises(module.RedeployContextMissingError):
            handler._on_gate_evaluated(envelope, envelope.correlation_id)

    def test_still_accepts_a_digest_only_decision_with_an_echoed_command(self) -> None:
        """A digest-only promotion names its artifact AND its lane, so it stays legal.

        The lane comes from the echoed gate command rather than the field
        default: ``ModelRedeployStartCommand.runtime_lane`` defaults to ``dev``,
        and a decision that silently deploys the dev lane because nobody said
        otherwise is the same defect class as the ref (AC4).
        """
        correlation_id = uuid4()
        payload = {
            "decision": self._bare_decision(image_digest="sha256:" + "a" * 64),
            "command": ModelProdPromotionGateCommand(
                correlation_id=correlation_id,
                runtime_lane=EnumRuntimeLane.PROD,
                requested_image_digest="sha256:" + "a" * 64,
            ).model_dump(mode="json"),
        }
        _, start, _ = module._coerce_gate_result(payload, correlation_id)
        assert start.image_digest == "sha256:" + "a" * 64
        assert start.git_ref is None
        assert start.runtime_lane is EnumRuntimeLane.PROD

    def test_accepts_a_decision_that_echoes_its_context(self) -> None:
        """The OMN-16939 path is untouched: the real ref rides through."""
        payload = {
            **self._bare_decision(),
            "deploy_context": ModelRedeployDeployContext(
                scope=EnumRedeployScope.FULL,
                git_ref="6c3acf69086868",
                runtime_lane=EnumRuntimeLane.DEV,
                build_source=EnumBuildSource.WORKSPACE,
                requested_by="gha/omnibase_infra/pr-3242",
            ).model_dump(mode="json"),
        }
        _, start, _ = module._coerce_gate_result(payload, uuid4())
        assert start.git_ref == "6c3acf69086868"
        assert start.build_source is EnumBuildSource.WORKSPACE
        assert start.requested_by == "gha/omnibase_infra/pr-3242"


class TestSharedCommandCoercion:
    """AC3: the shared ModelRedeployCommand no longer picks up a ref literal."""

    def test_refuses_a_shared_command_naming_no_artifact(self) -> None:
        correlation_id = uuid4()
        shared = ModelRedeployCommand(
            correlation_id=correlation_id,
            requested_at=datetime.now(UTC),
            runtime_lane=EnumRuntimeLane.DEV,
        )
        with pytest.raises(module.RedeployContextMissingError) as excinfo:
            module._coerce_start(shared, correlation_id)
        assert "git_ref" in str(excinfo.value)
        assert "image_digest" in str(excinfo.value)

    def test_accepts_a_shared_command_pinning_a_digest(self) -> None:
        """The closeout orchestrator's prod path pins a digest, not a ref."""
        correlation_id = uuid4()
        shared = ModelRedeployCommand(
            correlation_id=correlation_id,
            requested_at=datetime.now(UTC),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest="sha256:" + "b" * 64,
            promotion_batch_id="batch-omn18121",
        )
        start = module._coerce_start(shared, correlation_id)
        assert start.git_ref is None
        assert start.image_digest == "sha256:" + "b" * 64
        assert start.runtime_lane is EnumRuntimeLane.PROD


def _gate_evaluated_envelope(payload: dict[str, object]) -> object:
    """The envelope the runtime really builds: the ALIAS, not the topic (OMN-18013)."""
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

    from omnimarket.testing.publisher_contract_fixture import publisher_event_type

    return ModelEventEnvelope(
        payload=ModelProdPromotionGateDecision.model_validate(payload),
        correlation_id=uuid4(),
        event_type=publisher_event_type(
            "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
        ),
    )
