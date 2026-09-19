# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic constants for the lab lane-health projection (OMN-18769).

Every name here is READ OUT OF ``contract.yaml`` at import, never spelled in
this file. The first revision of this module declared the same six topics as
string literals beside a runtime check that the contract agreed with them, and
that shape is refused by the imperative-contract guard for a reason this node
cannot argue with: two spellings of one topic is a wiring fact that can drift,
and a consistency check between them only converts the drift into a startup
error on the lane where it is least convenient to discover. The contract is the
single authority; this module is a typed VIEW of it.

The positional reads below are pinned by ``_load`` raising on an unexpected
count, so a contract that gains or loses a subscription fails here -- loudly,
at import, in every test -- rather than silently repointing a constant.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

_CONTRACT: dict[str, object] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _contract_dlq_topics() -> tuple[str, ...]:
    """Read the contract's declared DLQ topics.

    ``contract_topics`` has helpers for subscribe and publish but not for
    the DLQ block, and adding one there would widen a shared module for one
    caller. Read locally, from the same parsed contract, never a literal.
    """
    bus = _CONTRACT.get("event_bus")
    if not isinstance(bus, dict):
        raise ValueError(f"{CONTRACT_PATH} declares no event_bus block")
    topics = bus.get("dlq_topics") or ()
    return tuple(str(topic) for topic in topics)


_SUBSCRIBE = contract_subscribe_topics(CONTRACT_PATH)
if len(_SUBSCRIBE) != 3:
    raise ValueError(
        "node_projection_lab_lane_health must subscribe to exactly three "
        f"topics -- the census, health and receipt facts; found {len(_SUBSCRIBE)}: "
        f"{_SUBSCRIBE}"
    )

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 1:
    raise ValueError(
        "node_projection_lab_lane_health must publish exactly one applied "
        f"topic; found {len(_PUBLISH)}: {_PUBLISH}"
    )

#: The census fact. The census refresh publishes on EVERY run, drift or no
#: drift -- a topic that only carries drift cannot distinguish "the lane is
#: clean" from "the census has not run", and the lane-health row needs both
#: answers (AC1). The pre-existing drift topic stays the ALERT authority and is
#: not consumed here.
TOPIC_LANE_CENSUS = _SUBSCRIBE[0]

#: The runtime's own health monitor's event. Pre-existing topic (OMN-15217);
#: what OMN-18769 adds upstream is the ``lane`` field, without which the event
#: cannot be keyed onto a lane row at all.
TOPIC_RUNTIME_HEALTH = _SUBSCRIBE[1]

#: The lab-pass receipt's verdict and check set, published at the moment the
#: artifact is written. The ARTIFACT stays the durable evidence; this event is
#: what makes it renderable (AC3).
TOPIC_LAB_PASS_RECEIPT = _SUBSCRIBE[2]

#: What this projection publishes when it has applied a fold.
TOPIC_PROJECTION_APPLIED = _PUBLISH[0]

_DLQ = _contract_dlq_topics()
if len(_DLQ) != 1:
    raise ValueError(
        "node_projection_lab_lane_health must declare exactly one DLQ topic; "
        f"found {len(_DLQ)}: {_DLQ}"
    )

#: Malformed source facts. A dropped event would leave a lane silently frozen
#: at its last good fact, which is the failure this projection exists to make
#: visible.
TOPIC_DLQ = _DLQ[0]

#: The bus-backed exposure the dashboard reads. Taken from the contract's own
#: ``projection_api`` block through the SAME loader the serving path uses, so
#: the exposure this module names and the exposure the API serves cannot be two
#: different topics.
_EXPOSURES = load_projection_exposures_from_contract(
    _CONTRACT,
    "node_projection_lab_lane_health",
    CONTRACT_PATH,
)
_BUS_BACKED = [exposure for exposure in _EXPOSURES if exposure.bus_backed]
if len(_BUS_BACKED) != 1:
    raise ValueError(
        "node_projection_lab_lane_health must declare exactly one bus_backed "
        f"exposure; found {len(_BUS_BACKED)}"
    )
TOPIC_EXPOSURE = _BUS_BACKED[0].topic

SUBSCRIBE_TOPICS: tuple[str, ...] = _SUBSCRIBE

__all__: list[str] = [
    "CONTRACT_PATH",
    "SUBSCRIBE_TOPICS",
    "TOPIC_DLQ",
    "TOPIC_EXPOSURE",
    "TOPIC_LAB_PASS_RECEIPT",
    "TOPIC_LANE_CENSUS",
    "TOPIC_PROJECTION_APPLIED",
    "TOPIC_RUNTIME_HEALTH",
]
