# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge S5, S6, rerouting and memory advice before claiming a replacement."""

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.models.item_state_check import (
    ModelItemPolicy,
    ModelItemRecoveryPlan,
    ModelItemRecoveryRequest,
)


class HandlerItemRecoveryPlan:
    def __init__(self, policy: ModelItemPolicy | None = None) -> None:
        self.policy = policy or ModelItemPolicy.model_validate(
            packaged_contract_value(
                "omnimarket.nodes.node_item_unanswered_compute", "config"
            )
        )

    def handle(self, request: ModelItemRecoveryRequest) -> ModelItemRecoveryPlan:
        item = request.item
        findings = tuple(
            f
            for f in request.findings
            if f.item_id == item.item_id and f.digest == item.digest
        )
        if not findings:
            raise ValueError(
                "recovery requires a finding for the current item revision"
            )
        advice = tuple(
            r
            for r in request.recommendations
            if r.item_id == item.item_id and r.digest == item.digest
        )
        repositories = {r.repository for r in advice if r.repository}
        if len(repositories) > 1:
            raise ValueError("conflicting repository recommendations")
        attempt = item.ended_lane.attempt + 1 if item.ended_lane else 0
        escalation = attempt > self.policy.retry_limit
        attempt = min(attempt, self.policy.retry_limit + 1)
        action = (
            self.policy.operator_action
            if item.owner == self.policy.operator_owner
            else self.policy.actions[item.kind]
        )
        if escalation:
            action = self.policy.escalation_action
        answer = item.kind == "message" and item.owner != self.policy.operator_owner
        if answer and (not item.session_id or not item.message_id):
            raise ValueError(
                "answer recovery requires canonical session and message ids"
            )
        cause = (
            f"answer@{item.session_id}@{item.message_id}"
            if answer
            else f"{item.family}#{attempt}"
        )
        return ModelItemRecoveryPlan(
            item_id=item.item_id,
            digest=item.digest,
            family=item.family,
            cause=cause,
            action=action,
            attempt=attempt,
            repository=next(iter(repositories), item.repository),
            memory_limit_mb=max(
                (
                    item.memory_limit_mb,
                    *(r.memory_limit_mb for r in advice if r.memory_limit_mb),
                )
            ),
            brief=item.words,
            escalation=escalation,
            rule_ids=tuple(sorted({f.rule_id for f in findings})),
        )
