# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class-name to topic routing of the handoff workflow's own emissions (OMN-20636).

The runtime publishes each event the handler returns on the topic its
contract's ``published_events`` row names for the class (``event_type`` is the
class name minus its ``Model`` prefix). This map is that block in Python, held
equal to the contract by a unit test, and :func:`publish_topic_for` is what the
chain tests publish through, so a test bus carries each event on the topic the
runtime would.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from omnimarket.events.topics import (
    PR_HANDOFF_ACCEPTED_TOPIC_V1,
    PR_HANDOFF_FAILED_TOPIC_V1,
    PR_HANDOFF_HANDED_OFF_TOPIC_V1,
    PR_HANDOFF_LEDGER_APPEND_REQUESTED_TOPIC_V1,
)
from omnimarket.models.pr_handoff import (
    ModelPrHandoffAccepted,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffLedgerAppendCommand,
)

PR_HANDOFF_EVENT_TOPICS: Mapping[type[BaseModel], str] = {
    ModelPrHandoffAccepted: PR_HANDOFF_ACCEPTED_TOPIC_V1,
    ModelPrHandoffHandedOff: PR_HANDOFF_HANDED_OFF_TOPIC_V1,
    ModelPrHandoffFailed: PR_HANDOFF_FAILED_TOPIC_V1,
    ModelPrHandoffLedgerAppendCommand: PR_HANDOFF_LEDGER_APPEND_REQUESTED_TOPIC_V1,
}


def publish_topic_for(event: BaseModel) -> str:
    """The contract topic the runtime publishes ``event`` on."""
    try:
        return PR_HANDOFF_EVENT_TOPICS[type(event)]
    except KeyError as exc:
        msg = (
            f"{type(event).__name__} is not an emission of node_pr_handoff_orchestrator"
        )
        raise ValueError(msg) from exc


__all__: list[str] = ["PR_HANDOFF_EVENT_TOPICS", "publish_topic_for"]
