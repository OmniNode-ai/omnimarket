# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate the coverage of every topic use a change adds to a node contract by a declared grant (OMN-24005).

Pure: the operations a direction needs are the render contract's config, never a constant here. A topic use is covered when
every operation its direction needs has some declared row for it. Only uses absent from the base branch's contracts are judged,
so no list of exceptions exists to grow.
"""

from __future__ import annotations

from omnimarket.models.broker_acl import (
    EnumAclPattern,
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    EnumTopicDirection,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclCoverageResult,
    ModelBrokerAclFinding,
    ModelBrokerAclRow,
    ModelContractTopicUse,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    contract_config,
)


class HandlerBrokerAclCoverageGate:
    def handle(
        self, request: ModelBrokerAclCoverageRequest
    ) -> ModelBrokerAclCoverageResult:
        config = contract_config()
        needed = {
            EnumTopicDirection.PUBLISH: set(config["derive_publish_operations"]),
            EnumTopicDirection.SUBSCRIBE: set(config["derive_subscribe_operations"]),
        }

        def covers(row: ModelBrokerAclRow, topic: str, operation: str) -> bool:
            if row.resource_type != EnumAclResourceType.TOPIC:
                return False
            if row.operation.value != operation:
                return False
            if row.pattern == EnumAclPattern.LITERAL:
                return row.name == topic
            return topic.startswith(row.name)

        def covered(use: ModelContractTopicUse) -> bool:
            return all(
                any(covers(row, use.topic, operation) for row in request.declared_rows)
                for operation in needed[use.direction]
            )

        added = sorted(
            set(request.topic_uses) - set(request.base_topic_uses),
            key=lambda use: (use.node, use.direction.value, use.topic),
        )
        findings = tuple(
            ModelBrokerAclFinding(
                code=EnumBrokerAclFindingCode.TOPIC_OPERATION_WITHOUT_ROW,
                detail=f"{use.node} {use.direction.value}: {use.topic}",
            )
            for use in added
            if not covered(use)
        )
        return ModelBrokerAclCoverageResult(
            status="refused" if findings else "covered",
            new_uses=len(added),
            uncovered_new_uses=len(findings),
            findings=findings,
        )
