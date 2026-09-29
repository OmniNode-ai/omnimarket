# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived topics for the board probe-results projection."""

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
    bus = _CONTRACT.get("event_bus")
    if not isinstance(bus, dict):
        raise ValueError(f"{CONTRACT_PATH} declares no event_bus block")
    topics = bus.get("dlq_topics") or ()
    return tuple(str(topic) for topic in topics)


_SUBSCRIBE = contract_subscribe_topics(CONTRACT_PATH)
if len(_SUBSCRIBE) != 1:
    raise ValueError(
        "node_projection_board_probe_results must subscribe to exactly one "
        f"topic; found {len(_SUBSCRIBE)}: {_SUBSCRIBE}"
    )
TOPIC_BOARD_PROBE_RESULT = _SUBSCRIBE[0]

_PUBLISH = contract_publish_topics(CONTRACT_PATH)
if len(_PUBLISH) != 1:
    raise ValueError(
        "node_projection_board_probe_results must publish exactly one applied "
        f"topic; found {len(_PUBLISH)}: {_PUBLISH}"
    )
TOPIC_PROJECTION_APPLIED = _PUBLISH[0]

_DLQ = _contract_dlq_topics()
if len(_DLQ) != 1:
    raise ValueError(
        "node_projection_board_probe_results must declare exactly one DLQ "
        f"topic; found {len(_DLQ)}: {_DLQ}"
    )
TOPIC_DLQ = _DLQ[0]

_EXPOSURES = load_projection_exposures_from_contract(
    _CONTRACT,
    "node_projection_board_probe_results",
    CONTRACT_PATH,
)
_BUS_BACKED = [exposure for exposure in _EXPOSURES if exposure.bus_backed]
if len(_BUS_BACKED) != 1:
    raise ValueError(
        "node_projection_board_probe_results must declare exactly one "
        f"bus-backed exposure; found {len(_BUS_BACKED)}"
    )
TOPIC_EXPOSURE = _BUS_BACKED[0].topic

SUBSCRIBE_TOPICS: tuple[str, ...] = _SUBSCRIBE

__all__ = [
    "CONTRACT_PATH",
    "SUBSCRIBE_TOPICS",
    "TOPIC_BOARD_PROBE_RESULT",
    "TOPIC_DLQ",
    "TOPIC_EXPOSURE",
    "TOPIC_PROJECTION_APPLIED",
]
