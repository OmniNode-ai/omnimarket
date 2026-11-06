# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check release authorization afresh at each gate boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from omnimarket.models.release_gate.model_release_gate_policy import (
    ModelReleaseGatePolicy,
)
from omnimarket.nodes.node_release_action_classify_compute.handlers.handler_release_action_classify import (
    HandlerReleaseActionClassify,
)
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseDecision,
    EnumReleaseEvent,
    EnumReleaseKind,
    ModelReleaseActionDecision,
    ModelReleaseActionRequest,
)
from omnimarket.nodes.node_release_grant_read_effect.handlers.handler_read_release_grant import (
    HandlerReadReleaseGrant,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelCheckReleaseGateRequest,
    ModelCheckReleaseGateResult,
    ModelReadReleaseGrantRequest,
)


class HandlerCheckReleaseGate:
    """Load policy and grant facts for this call without caching authorization."""

    def __init__(
        self,
        reader: HandlerReadReleaseGrant | None = None,
        policy_loader: Callable[[Path | None], ModelReleaseGatePolicy] | None = None,
        classifier: HandlerReleaseActionClassify | None = None,
    ) -> None:
        self._reader = reader
        self._policy_loader = policy_loader
        self._classifier = (
            classifier if classifier is not None else HandlerReleaseActionClassify()
        )

    def handle(
        self, request: ModelCheckReleaseGateRequest
    ) -> ModelCheckReleaseGateResult:
        """Check current authorization, failing closed when policy is unavailable."""
        if self._reader is None or self._policy_loader is None:
            return self._blocked("gate_not_wired")
        try:
            policy = self._policy_loader(
                Path(request.head_checkout) if request.head_checkout else None
            )
        except Exception:
            return self._blocked("policy_unreadable")

        if (
            request.event is EnumReleaseEvent.PULL_REQUEST
            and request.pull_request is None
        ):
            return self._blocked("pull_request_facts_missing")

        grant = None
        if request.pull_request is not None and (
            request.event is EnumReleaseEvent.PULL_REQUEST
        ):
            grant = self._reader.handle(
                ModelReadReleaseGrantRequest(
                    repository=request.repository,
                    pull_request=request.pull_request.number,
                    head_sha=request.pull_request.head_sha,
                    boundary=request.boundary,
                    policy=policy,
                )
            )
        decision = self._classifier.handle(
            ModelReleaseActionRequest(
                repository=request.repository,
                event=request.event,
                actor=request.actor,
                policy=policy,
                ref=request.ref,
                pull_request=request.pull_request,
                changed_files=request.changed_files,
                grant=grant,
            )
        )
        exit_code = {
            EnumReleaseDecision.ADMIT: 0,
            EnumReleaseDecision.REFUSE: 1,
            EnumReleaseDecision.BLOCKED: 2,
        }[decision.decision]
        return ModelCheckReleaseGateResult(
            decision=decision, grant=grant, exit_code=exit_code
        )

    @staticmethod
    def _blocked(reason: str) -> ModelCheckReleaseGateResult:
        return ModelCheckReleaseGateResult(
            decision=ModelReleaseActionDecision(
                kind=EnumReleaseKind.NONE,
                decision=EnumReleaseDecision.BLOCKED,
                reason=reason,
                line=f"BLOCKED none: {reason}",
            ),
            grant=None,
            exit_code=2,
        )
