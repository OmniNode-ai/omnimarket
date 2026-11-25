# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only dry run: plan which topics a runtime must provision, without writing to the broker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext

from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
    AiokafkaBrokerTopicCreator,
    AiokafkaBrokerTopicLister,
    ProtocolBrokerTopicLister,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_writer_topic_apply import (
    HandlerWriterTopicApply,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)
from omnimarket.nodes.node_topic_provision_plan_compute.overlay import (
    load_provision_overlay,
    load_provision_rule,
)

BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"


def main(argv: Sequence[str] | None = None) -> int:
    """Load the overlay and rule, run the dry run (or, with --apply, the writer topic apply), print one JSON line."""
    parser = argparse.ArgumentParser(prog="node_topic_provision_dry_run_effect")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create each planned service writer topic the broker does not hold (1 partition, 1 replica)",
    )
    args = parser.parse_args(argv)
    overlay = load_provision_overlay()
    if not overlay.overlay_roots:
        overlay = overlay.model_copy(
            update={
                "overlay_roots": tuple(
                    root
                    for root in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep)
                    if root
                )
            }
        )
    rule = load_provision_rule()
    lister: ProtocolBrokerTopicLister | None = None
    creator: AiokafkaBrokerTopicCreator | None = None
    binding: AbstractContextManager[object] = nullcontext()
    if overlay.lane:
        # The lane's declared broker and this machine's identity for it, the way
        # `onex scm` resolves them; the lister reads the bound transport.
        selection, transport = resolve_lane_binding(overlay.lane)
        if transport is not None:
            binding = bind_lane_client_transport(transport)
        lister = AiokafkaBrokerTopicLister(selection.bootstrap_servers)
        creator = AiokafkaBrokerTopicCreator(selection.bootstrap_servers)
    elif os.environ.get(BOOTSTRAP_ENV):
        # No declared lane: the standard runtime Kafka environment answers for this broker.
        lister = AiokafkaBrokerTopicLister(os.environ[BOOTSTRAP_ENV])
        creator = AiokafkaBrokerTopicCreator(os.environ[BOOTSTRAP_ENV])
    request = ModelTopicProvisionDryRunRequest(overlay=overlay, rule=rule)
    if args.apply:
        if lister is None or creator is None:
            raise SystemExit(
                f"--apply needs overlay.lane or {BOOTSTRAP_ENV}: the broker to create on"
            )
        with binding:
            result = asyncio.run(
                HandlerWriterTopicApply(lister, creator).handle(request)
            )
        sys.stdout.write(json.dumps(result.model_dump(mode="json")) + "\n")
        return 0
    handler = HandlerTopicProvisionDryRun(lister)
    with binding:
        plan = asyncio.run(handler.handle(request))
    sys.stdout.write(
        json.dumps(
            {
                "profile": plan.profile,
                "mode": plan.mode,
                "contracts_total": len(plan.contracts_loaded)
                + len(plan.contracts_skipped),
                "contracts_loaded": len(plan.contracts_loaded),
                "contracts_skipped": len(plan.contracts_skipped),
                "declared_topic_count": plan.declared_topic_count,
                "topics_to_provision": len(plan.topics_to_provision),
                "live_topic_count": plan.live_topic_count,
                "live_topics_under_rule": plan.live_topics_under_rule,
                "live_topics_outside_rule": plan.live_topics_outside_rule,
                "live_topics_undeclared": plan.live_topics_undeclared,
                "service_writer_topics": list(plan.service_writer_topics),
                "host_unit_publish_topics": list(plan.host_unit_publish_topics),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
