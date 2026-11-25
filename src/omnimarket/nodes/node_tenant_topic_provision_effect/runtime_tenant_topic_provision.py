# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry: provision the topics of one or more tenants on a lane's broker and print one JSON line.

``--tenant`` is repeated for each tenant. One tenant is a single command; several are a backfill over them. The lane, the
partition count and the replication factor come from the node's settings unless all three flags are given. With
``--consumer-read-back`` a consumer on the same broker then looks for every topic the run read back, and the line names
the ones it saw. Exit 0 when every tenant is provisioned, 1 when any is not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import uuid4

from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)

from omnimarket.nodes.node_tenant_topic_provision_effect.adapters.consumer_read_back import (
    consumer_visible_topics,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.adapters.in_memory_tenant_marks import (
    InMemoryTenantTopicMarks,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.broker_settings import (
    ModelTenantTopicProvisionSettings,
    load_settings,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.handlers.handler_tenant_topic_provision import (
    HandlerTenantTopicProvision,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicOutcome,
    ModelTenantTopicProvisionCommand,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
    AiokafkaBrokerTopicCreator,
    AiokafkaBrokerTopicLister,
)

_OK = {EnumTenantTopicOutcome.PROVISIONED, EnumTenantTopicOutcome.ALREADY_PROVISIONED}


def _settings(args: argparse.Namespace) -> ModelTenantTopicProvisionSettings:
    if args.lane and args.partitions and args.replicas:
        return ModelTenantTopicProvisionSettings(
            lane=args.lane,
            num_partitions=args.partitions,
            replication_factor=args.replicas,
        )
    return load_settings()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_tenant_topic_provision_effect")
    parser.add_argument("--tenant", action="append", required=True)
    parser.add_argument("--lane")
    parser.add_argument("--partitions", type=int)
    parser.add_argument("--replicas", type=int)
    parser.add_argument("--consumer-read-back", action="store_true")
    args = parser.parse_args(argv)
    settings = _settings(args)
    selection, transport = resolve_lane_binding(settings.lane)
    if transport is None:
        raise SystemExit(f"lane {settings.lane} resolves no client transport")
    tenants = tuple(args.tenant)
    handler = HandlerTenantTopicProvision(
        registry=InMemoryTenantTopicMarks(tenants),
        lister=AiokafkaBrokerTopicLister(selection.bootstrap_servers),
        creator=AiokafkaBrokerTopicCreator(selection.bootstrap_servers),
        num_partitions=settings.num_partitions,
        replication_factor=settings.replication_factor,
        broker_binding=bind_lane_client_transport(transport),
    )
    command = (
        ModelTenantTopicProvisionCommand(command_id=uuid4(), tenant_slug=tenants[0])
        if len(tenants) == 1
        else ModelTenantTopicProvisionCommand(command_id=uuid4(), backfill=True)
    )

    async def run() -> dict[str, object]:
        result = await handler.handle(command)
        line: dict[str, object] = json.loads(result.model_dump_json())
        read_back = [t for report in result.tenants for t in report.read_back]
        if args.consumer_read_back:
            if not read_back:
                line["consumer_saw"] = []
                return line
            with bind_lane_client_transport(transport):
                line["consumer_saw"] = list(
                    await consumer_visible_topics(
                        selection.bootstrap_servers, read_back
                    )
                )
        return line

    line = asyncio.run(run())
    sys.stdout.write(json.dumps(line) + "\n")
    return 0 if line["outcome"] in {o.value for o in _OK} else 1


if __name__ == "__main__":
    raise SystemExit(main())
