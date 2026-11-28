# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topic constants for the lane work projection (OMN-26708).

Every name is read out of ``contract.yaml`` at import; none is spelled here.
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

_SUBSCRIBE = contract_subscribe_topics(CONTRACT_PATH)
if len(_SUBSCRIBE) != 1:
    raise ValueError(
        "node_projection_lane_work must subscribe to exactly the transition "
        f"topic; found {len(_SUBSCRIBE)}: {_SUBSCRIBE}"
    )

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 1:
    raise ValueError(
        f"node_projection_lane_work must publish exactly one applied topic; found {_PUBLISH}"
    )

#: The orchestrator's transition receipts: the one input this projection folds.
TOPIC_LANE_WORK_TRANSITION = _SUBSCRIBE[0]

#: What this projection publishes when it has applied a fold.
TOPIC_PROJECTION_APPLIED = _PUBLISH[0]

_EXPOSURES = load_projection_exposures_from_contract(
    _CONTRACT, "node_projection_lane_work", CONTRACT_PATH
)
_TOPICS = [exposure.topic for exposure in _EXPOSURES]
if len(_TOPICS) != 2 or {exposure.table for exposure in _EXPOSURES} != {
    "lane_work_workflow"
}:
    raise ValueError(
        "node_projection_lane_work must expose lane_work_workflow twice (current "
        f"state by workflow, then the terminal rows); found {_TOPICS}"
    )

#: Query one: the current state of every workflow, keyed by workflow id.
TOPIC_EXPOSURE_WORKFLOW = _TOPICS[0]

#: Query two: the terminal rows, the orchestrator's view. The writer publishes
#: to it only a row whose terminal_kind is set.
TOPIC_EXPOSURE_TERMINAL = _TOPICS[1]
