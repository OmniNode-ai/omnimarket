# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Detect the bus-binding exception classes from their recorded evidence (OMN-21959).

Two readers, both pure:

* ``read_tick_evidence`` reads a recorded tick line and the broker error beside it. A tick that reports
  ``state=healthy`` while the broker answered an unknown topic or partition error names the topic, so the
  reducer can refuse the healthy report.
* ``classify_block`` recognises an exception-queue evidence block as one of the three bus-binding classes from
  the words the producers write, not from the class label it carries.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from omnimarket.handlers.bus_binding.recovery import (
    ALARM_CLASS,
    OUTAGE_CLASS,
    REFUSED_CLASS,
)
from omnimarket.handlers.bus_binding.reducer import (
    reduce_binding_state,
    topic_degraded_reason,
)
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
    ModelBusBindingTickEvidence,
)

_TICK = re.compile(
    r"bus-binding lane=(?P<lane>\S+) binding=(?P<binding>durable|inmemory) "
    r"state=(?P<state>healthy|degraded|recovering) pending_outbox=(?P<pending>\d+)"
)
_UNKNOWN_TOPIC = re.compile(
    r"unknown topic or partition[^\n]*?topic='(?P<topic>[^']+)'", re.IGNORECASE
)
_OUTAGE_EVIDENCE = re.compile(r"^outage_seconds=\S+; durable bus health read failed")
_HEALTH_READ_EVIDENCE = "durable bus health read failed"
_REFUSAL_EVIDENCE = "refused the queued append"


def read_tick_evidence(text: str) -> ModelBusBindingTickEvidence | None:
    """The binding a recorded tick line reports and the topics the broker said are unknown, or None without a tick."""
    tick = _TICK.search(text)
    if tick is None:
        return None
    missing = tuple(dict.fromkeys(m["topic"] for m in _UNKNOWN_TOPIC.finditer(text)))
    return ModelBusBindingTickEvidence(
        lane=tick["lane"],
        binding=EnumBusBinding(tick["binding"]),
        state=EnumBusBindingState(tick["state"]),
        pending_outbox=int(tick["pending"]),
        missing_topics=missing,
    )


def decide_from_tick_evidence(
    evidence: ModelBusBindingTickEvidence,
    *,
    prior: EnumBusBindingState,
    observed_at: datetime,
) -> ModelBusBindingDecision:
    """The decision the reducer reaches from the recorded facts; a queued outbox and an unknown topic is degraded."""
    state, binding = reduce_binding_state(
        prior,
        durable_healthy=evidence.state is not EnumBusBindingState.DEGRADED,
        pending_outbox=evidence.pending_outbox,
        missing_topics=evidence.missing_topics,
    )
    degraded_by_topic = (
        state is EnumBusBindingState.DEGRADED
        and evidence.state is not EnumBusBindingState.DEGRADED
    )
    return ModelBusBindingDecision(
        lane=evidence.lane,
        binding=binding,
        state=state,
        prior_state=prior,
        reason=topic_degraded_reason(evidence.missing_topics)
        if degraded_by_topic
        else "",
        observed_at=observed_at,
        pending_outbox=evidence.pending_outbox,
    )


def classify_block(row: Mapping[str, object]) -> str | None:
    """The bus-binding class an evidence block belongs to, from its evidence text; None for any other case."""
    evidence = str(row.get("evidence", ""))
    if _OUTAGE_EVIDENCE.match(evidence):
        return OUTAGE_CLASS
    if _HEALTH_READ_EVIDENCE in evidence:
        return ALARM_CLASS
    if _REFUSAL_EVIDENCE in evidence:
        return REFUSED_CLASS
    return None
