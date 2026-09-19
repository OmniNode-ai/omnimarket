# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared topic constants for the lab lane-health projection (OMN-18769).

The node's ``contract.yaml`` remains the wiring authority; this module is the
code-side constant registry the handlers import, and
``HandlerProjectionLabLaneHealth.subscribe_topics`` refuses to start if the two
disagree. Two places is one more than ideal, and the disagreement check is what
makes it safe: a topic declared in one and not the other would otherwise
surface as a silently unconsumed subscription.
"""

from __future__ import annotations

#: The census fact. The census refresh publishes here on EVERY run, drift or no
#: drift -- a topic that only carries drift cannot distinguish "the lane is
#: clean" from "the census has not run", and the lane-health row needs both
#: answers (AC1). The pre-existing drift topic stays the ALERT authority and is
#: not consumed here.
TOPIC_LANE_CENSUS = "onex.evt.omnibase-infra.lane-census-observed.v1"

#: The runtime's own health monitor's event. Pre-existing topic (OMN-15217);
#: what OMN-18769 adds upstream is the ``lane`` field, without which the event
#: cannot be keyed onto a lane row at all.
TOPIC_RUNTIME_HEALTH = "onex.evt.omnibase-infra.runtime-health-check.v1"

#: The lab-pass receipt's verdict and check set, published at the moment the
#: artifact is written. The ARTIFACT stays the durable evidence; this event is
#: what makes it renderable (AC3).
TOPIC_LAB_PASS_RECEIPT = "onex.evt.omnibase-infra.lab-pass-receipt.v1"

#: What this projection publishes when it has applied a fold.
TOPIC_PROJECTION_APPLIED = "onex.evt.omnimarket.projection-lab-lane-health-applied.v1"

#: Malformed source facts. A dropped event would leave a lane silently frozen
#: at its last good fact, which is the failure this projection exists to make
#: visible.
TOPIC_DLQ = "onex.dlq.omnimarket.projection-lab-lane-health-malformed.v1"

#: The bus-backed exposure the dashboard reads.
TOPIC_EXPOSURE = "onex.snapshot.projection.lab.lane-health.v1"

SUBSCRIBE_TOPICS: tuple[str, ...] = (
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
    TOPIC_LAB_PASS_RECEIPT,
)

__all__: list[str] = [
    "SUBSCRIBE_TOPICS",
    "TOPIC_DLQ",
    "TOPIC_EXPOSURE",
    "TOPIC_LAB_PASS_RECEIPT",
    "TOPIC_LANE_CENSUS",
    "TOPIC_PROJECTION_APPLIED",
    "TOPIC_RUNTIME_HEALTH",
]
