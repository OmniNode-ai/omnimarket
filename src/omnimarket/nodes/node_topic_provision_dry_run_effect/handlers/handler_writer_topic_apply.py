# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWriterTopicApply: create each planned service writer topic the broker does not hold."""

from __future__ import annotations

from omnimarket.models.topic_provision.model_writer_topic_apply_result import (
    ModelWriterTopicApplyResult,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
    ProtocolBrokerTopicCreator,
    ProtocolBrokerTopicLister,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)

WRITER_TOPIC_PARTITIONS = 1
WRITER_TOPIC_REPLICAS = 1


class HandlerWriterTopicApply:
    """Plans with the dry run, then creates only the planned writer topics that are absent.

    The one write the provisioning makes: a topic the plan names under ``service_writer_topics``
    and the broker does not list is created with one partition and one replica. A topic the broker
    already holds, or that appears between the list and the create, is accepted, so a second run
    issues no create call.
    """

    def __init__(
        self, lister: ProtocolBrokerTopicLister, creator: ProtocolBrokerTopicCreator
    ) -> None:
        self._lister = lister
        self._creator = creator

    async def handle(
        self, request: ModelTopicProvisionDryRunRequest
    ) -> ModelWriterTopicApplyResult:
        plan = await HandlerTopicProvisionDryRun(self._lister).handle(request)
        live = set(await self._lister.list_topics())
        created: list[str] = []
        existing: list[str] = []
        for topic in plan.service_writer_topics:
            if topic in live:
                existing.append(topic)
            elif await self._creator.create_topic(
                topic, WRITER_TOPIC_PARTITIONS, WRITER_TOPIC_REPLICAS
            ):
                created.append(topic)
            else:
                existing.append(topic)
        return ModelWriterTopicApplyResult(
            planned=plan.service_writer_topics,
            created=tuple(created),
            existing=tuple(existing),
            num_partitions=WRITER_TOPIC_PARTITIONS,
            replication_factor=WRITER_TOPIC_REPLICAS,
        )
