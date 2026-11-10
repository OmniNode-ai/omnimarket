# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan the broker topics a running profile's loaded contracts need (OMN-23614).

A topic is created only for a contract a running profile loads: the caller resolves
which contracts the profile and the lane manifest load and passes the names, and this
handler unions the rule-allowed topics of the loaded contracts only. A loaded name no
registry contract holds is refused by name, and an empty loaded set is refused outright
— the plan never provisions on a guess. The rule itself (which topic kinds count, the
partitions a not-yet-created topic gets, the partition bar) comes from the node overlay,
never from code.
"""

from __future__ import annotations

from ..models.model_topic_provision_plan import (
    ModelTopicProvisionPlanRequest,
    ModelTopicProvisionPlanResult,
)


class HandlerTopicProvisionPlan:
    def handle(
        self, request: ModelTopicProvisionPlanRequest
    ) -> ModelTopicProvisionPlanResult:
        names = sorted(set(request.loaded_contract_names))
        known = {contract.contract_name for contract in request.contracts}
        if not names:
            return ModelTopicProvisionPlanResult(
                status="refused",
                refusal_reasons=("no_loaded_contracts",),
                contracts_total=len(request.contracts),
            )
        unknown = tuple(
            f"unknown_loaded_contract:{name}" for name in names if name not in known
        )
        if unknown:
            return ModelTopicProvisionPlanResult(
                status="refused",
                refusal_reasons=unknown,
                contracts_total=len(request.contracts),
            )
        loaded = set(names)
        rule = request.rule
        allowed: set[str] = set()
        for contract in request.contracts:
            if contract.contract_name not in loaded:
                continue
            if rule.include_subscribe_topics:
                allowed.update(contract.subscribe_topics)
            if rule.include_publish_topics:
                allowed.update(contract.publish_topics)
        live_names = {topic.name for topic in request.live_topics}
        live_inside = [topic for topic in request.live_topics if topic.name in allowed]
        topics_to_create = len(allowed - live_names)
        projected_partitions = (
            sum(topic.partitions for topic in live_inside)
            + rule.default_partitions * topics_to_create
        )
        return ModelTopicProvisionPlanResult(
            status="planned",
            topics_to_provision=tuple(sorted(allowed)),
            excluded_contracts=tuple(sorted(known - loaded)),
            contracts_total=len(request.contracts),
            contracts_loaded=len(loaded),
            live_topics_total=len(request.live_topics),
            live_topics_inside_rule=len(live_inside),
            live_topics_outside_rule=len(live_names - allowed),
            topics_to_create=topics_to_create,
            would_exist_topics=len(live_inside) + topics_to_create,
            projected_partitions=projected_partitions,
            partition_bar=rule.partition_bar,
            under_partition_bar=projected_partitions <= rule.partition_bar,
        )
