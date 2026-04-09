# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for node_platform_readiness SOW Phase 2 validation."""

from __future__ import annotations

# SOW Phase 2 required Kafka topics — declared in their respective node contract.yaml files.
# These are validated by the kafka_topic_coverage readiness dimension.
SAVINGS_ESTIMATION_COMPLETED = "onex.evt.savings.estimation-completed.v1"
MODEL_ROUTER_ROUTING_DECISION = "onex.evt.model-router.routing-decision.v1"
BASELINES_COMPUTATION_COMPLETED = "onex.evt.baselines.computation-completed.v1"
BUILD_LOOP_DOD_CHECKED = "onex.evt.build-loop.dod-checked.v1"

SOW_PHASE2_REQUIRED_TOPICS: list[str] = [
    SAVINGS_ESTIMATION_COMPLETED,
    MODEL_ROUTER_ROUTING_DECISION,
    BASELINES_COMPUTATION_COMPLETED,
    BUILD_LOOP_DOD_CHECKED,
]
