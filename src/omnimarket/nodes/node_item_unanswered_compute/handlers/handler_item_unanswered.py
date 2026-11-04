# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 applies to all open kinds and states; grace is a label only."""

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.handlers.item_response import item_answered
from omnimarket.models.item_state_check import (
    ModelItemFinding,
    ModelItemFindings,
    ModelItemPolicy,
    ModelItemSnapshot,
)


class HandlerItemUnanswered:
    def __init__(self, policy: ModelItemPolicy | None = None) -> None:
        self.policy = policy or ModelItemPolicy.model_validate(
            packaged_contract_value(
                "omnimarket.nodes.node_item_unanswered_compute", "config"
            )
        )

    def handle(self, request: ModelItemSnapshot) -> ModelItemFindings:
        rule = self.policy.rules["S5"]
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
                if not item_answered(item, request, self.policy)
            )
        )
