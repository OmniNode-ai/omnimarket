# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate the coverage of every contract topic use by a declared row or a frozen baseline (OMN-24005).

Pure: the operations a direction needs are the render contract's config, never a constant here. A topic use is covered
when every operation its direction needs has some declared row for it; a node's uncovered uses are held at a baseline
of how many it had when the baseline was frozen, so the baseline only shrinks.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.models.broker_acl import (
    EnumAclPattern,
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    EnumTopicDirection,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclCoverageResult,
    ModelBrokerAclFinding,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    contract_config,
)

_CODES = EnumBrokerAclFindingCode


class HandlerBrokerAclCoverageGate:
    def handle(
        self, request: ModelBrokerAclCoverageRequest
    ) -> ModelBrokerAclCoverageResult:
        config = contract_config()
        publish_ops = set(config["derive_publish_operations"])
        subscribe_ops = set(config["derive_subscribe_operations"])

        def ops_for(direction_value: str) -> set[str]:
            return (
                publish_ops
                if direction_value == EnumTopicDirection.PUBLISH.value
                else subscribe_ops
            )

        def covers(row: ModelBrokerAclRow, topic: str, operation: str) -> bool:
            if row.resource_type != EnumAclResourceType.TOPIC:
                return False
            if row.operation.value != operation:
                return False
            if row.pattern == EnumAclPattern.LITERAL:
                return row.name == topic
            return topic.startswith(row.name)

        uses_by_node: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for use in request.topic_uses:
            uses_by_node[use.node].append((use.direction.value, use.topic))

        baseline_by_node: dict[str, int] = {}
        for entry in request.baseline:
            baseline_by_node[entry.node] = entry.uncovered_uses

        findings: list[ModelBrokerAclFinding] = []
        total_uncovered = 0
        baselined_uses = 0
        nodes_with_uses = 0

        for node in sorted(uses_by_node):
            uses = uses_by_node[node]
            nodes_with_uses += 1
            uncovered = [
                (direction, topic)
                for direction, topic in uses
                if not all(
                    any(covers(row, topic, op) for row in request.declared_rows)
                    for op in ops_for(direction)
                )
            ]
            uncovered.sort(key=lambda pair: (pair[0], pair[1]))
            total_uncovered += len(uncovered)
            baseline = baseline_by_node.get(node, 0)
            if len(uncovered) > baseline:
                for direction, topic in uncovered:
                    findings.append(
                        ModelBrokerAclFinding(
                            code=_CODES.TOPIC_OPERATION_WITHOUT_ROW,
                            detail=f"{node} {direction}: {topic}",
                        )
                    )
            elif len(uncovered) < baseline:
                findings.append(
                    ModelBrokerAclFinding(
                        code=_CODES.BASELINE_STALE,
                        detail=f"{node}: baseline holds {baseline}, contracts now need {len(uncovered)}",
                    )
                )
            else:
                baselined_uses += baseline

        for node in sorted(set(baseline_by_node) - set(uses_by_node)):
            baseline = baseline_by_node[node]
            findings.append(
                ModelBrokerAclFinding(
                    code=_CODES.BASELINE_STALE,
                    detail=f"{node}: baseline holds {baseline}, contracts now need 0",
                )
            )

        findings.sort(key=lambda finding: (finding.code.value, finding.detail))
        return ModelBrokerAclCoverageResult(
            status="refused" if findings else "covered",
            nodes=nodes_with_uses,
            uncovered_uses=total_uncovered,
            baselined_uses=baselined_uses,
            findings=tuple(findings),
        )
