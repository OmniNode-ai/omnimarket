# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S6 finds an owned open item whose work lane ended without done."""

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.handlers.item_response import delivered_response
from omnimarket.models.item_state_check import (
    ModelItemFinding,
    ModelItemFindings,
    ModelItemPolicy,
    ModelItemSnapshot,
)


class HandlerItemLaneMiss:
    def __init__(self, policy: ModelItemPolicy | None = None) -> None:
        self.policy = policy or ModelItemPolicy.model_validate(
            packaged_contract_value(
                "omnimarket.nodes.node_item_unanswered_compute", "config"
            )
        )

    def handle(self, request: ModelItemSnapshot) -> ModelItemFindings:
        rule = self.policy.rules["S6"]
        return ModelItemFindings(
            findings=tuple(
                ModelItemFinding(
                    item_id=item.item_id,
                    digest=item.digest,
                    class_key=rule.class_key,
                    rule_id=rule.rule_id,
                    label=item.state,
                )
                for item in request.items
                if item.state not in self.policy.closed_states
                and item.owner
                and item.owner != self.policy.operator_owner
                and not item.running_lane
                and item.ended_lane
                and not item.ended_lane.done
                and item.ended_lane.outcome not in self.policy.done_outcomes
                and not delivered_response(item, request, self.policy)
            )
        )
