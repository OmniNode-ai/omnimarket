# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only dry run: plan which topics a runtime must provision, without writing to the broker."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.broker_topics import (
    AiokafkaBrokerTopicLister,
    ProtocolBrokerTopicLister,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.models.model_topic_provision_dry_run_request import (
    ModelTopicProvisionDryRunRequest,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    TopicProvisionRuleError,
)
from omnimarket.nodes.node_topic_provision_plan_compute.overlay import (
    load_provision_overlay,
    load_provision_rule,
)


def main() -> int:
    """Load the overlay and rule, run the dry run, and print one JSON line of counts."""
    overlay = load_provision_overlay()
    rule = load_provision_rule()
    lister: ProtocolBrokerTopicLister | None = None
    if overlay.bootstrap_env:
        addr = os.environ.get(
            overlay.bootstrap_env, ""
        )  # node-purity-ok: boundary entry reads the broker address variable the overlay names
        if not addr:
            raise TopicProvisionRuleError(
                f"environment variable {overlay.bootstrap_env!r} is empty or unset"
            )
        lister = AiokafkaBrokerTopicLister(addr)
    handler = HandlerTopicProvisionDryRun(lister)
    plan = asyncio.run(
        handler.handle(ModelTopicProvisionDryRunRequest(overlay=overlay, rule=rule))
    )
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
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
