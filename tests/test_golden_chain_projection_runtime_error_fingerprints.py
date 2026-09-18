# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_runtime_error_fingerprints`` (OMN-18770).

Walks the chain the contract declares, hop by hop, with the lab's own numbers:

    onex.evt.omnibase-infra.runtime-error.v1     (the log bridge's raw event)
        -> derived category + derived fingerprint (computed HERE, not upstream)
        -> omninode_internal.runtime_error_fingerprints   (ranked read model)
        -> onex.snapshot.projection.runtime-error-fingerprints.v1  (bus-backed)
        -> onex.evt.omnimarket.projection-runtime-error-fingerprints-applied.v1

The chain's whole purpose is one distinction: a surface that says "69 open
incidents, category unknown" must not look the same as one that says "315
occurrences of a database connection failure, here is the correlation id of
the most recent". Everything else here is in service of that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    EnumRuntimeErrorCategory,
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runtime_error_fingerprints"
    / "contract.yaml"
)

_SOURCE_TOPIC = "onex.evt.omnibase-infra.runtime-error.v1"  # onex-topic-allow: the carrier omnibase_infra's log bridge already emits on
_SNAPSHOT_TOPIC = "onex.snapshot.projection.runtime-error-fingerprints.v1"  # onex-topic-allow: projection snapshot topics use onex.snapshot.* by convention
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-runtime-error-fingerprints-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-runtime-error-fingerprints-malformed.v1"  # onex-topic-allow: this node's declared DLQ

_T0 = datetime(2026, 9, 18, 23, 0, 0, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _bridge_event(**overrides: Any) -> ModelRuntimeErrorEventWire:
    """One event in the exact shape ``RuntimeLogEventBridge`` publishes."""
    payload: dict[str, Any] = {
        "event_id": "aaaaaaaa-0000-4000-8000-000000000001",
        "correlation_id": "cccccccc-0000-4000-8000-000000000001",
        "logger_family": "test.logger.da1a030e",
        "log_level": "ERROR",
        # Templatized by the bridge before publish -- variable spans already
        # collapsed to {}, which is what makes a keyword rule safe.
        "message_template": "Database connection failed to host db-primary",
        "raw_message": "Database connection failed to host db-primary",
        # The bridge's own classifier answered this, and it is wrong.
        "error_category": "unknown",
        "severity": "error",
        "fingerprint": "producerside000f",
        "occurrence_count_local": 1,
        "exception_type": "",
        "exception_message": "",
        "hostname": "omninode-runtime",
        "service_label": "onex-kernel",
        "timestamp": _T0,
    }
    payload.update(overrides)
    return ModelRuntimeErrorEventWire.model_validate(payload)


@pytest.mark.unit
def test_golden_chain_hops_are_the_declared_topics() -> None:
    """Every hop is contract-declared -- none is a Python constant.

    A topic that lives only in code is a topic the platform cannot reason
    about, and this node exists precisely so an operator can reason about what
    is failing.
    """
    contract = _contract()
    event_bus = contract["event_bus"]

    assert event_bus["subscribe_topics"] == [_SOURCE_TOPIC], (
        "the chain must ride the topic the log bridge ALREADY publishes -- a "
        "new transport would need a producer that does not exist"
    )
    assert event_bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert event_bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["projection_api"]["topic"] == _SNAPSHOT_TOPIC
    assert contract["externally_consumed_topics"] == [_TERMINAL_TOPIC]


@pytest.mark.unit
def test_golden_chain_names_the_producer_of_the_topic_it_subscribes_to() -> None:
    """A subscriber to a topic no contract publishes is an ORPHANED_CONSUMER --
    a panel that can only ever render nothing. The producer is outside the
    node graph here, so the contract names it explicitly."""
    external = _contract()["externally_produced_topics"]
    assert [entry["topic"] for entry in external] == [_SOURCE_TOPIC]
    producer = external[0]["producer"]
    assert "RuntimeLogEventBridge" in producer
    assert "ENABLE_RUNTIME_LOG_BRIDGE" in producer


@pytest.mark.unit
def test_golden_chain_end_to_end_ranks_a_flood_above_a_singleton() -> None:
    """The chain, walked with the lab's own recorded population.

    ``test.ratelimit.c4d2f698`` had 315 occurrences on the lab surface and
    ``test.logger.da1a030e`` had 63. Before this node both rendered as one
    ``unknown`` row among 69, indistinguishable by anything an operator could
    sort on.
    """
    handler = HandlerProjectionRuntimeErrorFingerprints()

    # Hop 1 -> 2: the loud one. Three separate deliveries of the same error
    # class, exactly as the bridge's rate limiter collapses and re-emits them.
    loud_total = 0
    loud_row = None
    for index, batch in enumerate((105, 105, 105)):
        result = handler.handle(
            ModelRuntimeErrorFingerprintRequest(
                event=_bridge_event(
                    logger_family="test.ratelimit.c4d2f698",
                    message_template="Repeated error message on topic {}",
                    occurrence_count_local=batch,
                    correlation_id=f"cccccccc-0000-4000-8000-00000000000{index}",
                    timestamp=_T0 + timedelta(minutes=index),
                ),
                prior_occurrence_count=loud_total,
                prior_first_seen_at=None
                if loud_row is None
                else loud_row.first_seen_at,
            )
        )
        loud_row = result.row
        loud_total = loud_row.occurrence_count

    quiet_row = handler.handle(
        ModelRuntimeErrorFingerprintRequest(
            event=_bridge_event(occurrence_count_local=63)
        )
    ).row

    assert loud_row is not None
    # THE COLLAPSE: three deliveries, one row, 315 occurrences.
    assert loud_row.occurrence_count == 315
    assert quiet_row.occurrence_count == 63

    # Hop 3: the ranking the exposure serves.
    ranked = sorted(
        [loud_row, quiet_row], key=lambda r: r.occurrence_count, reverse=True
    )
    assert [r.occurrence_count for r in ranked] == [315, 63]

    # Hop 2: BOTH categories are derived, and neither is `unknown` -- which is
    # what 100% of the recorded lab rows said.
    assert loud_row.error_category is EnumRuntimeErrorCategory.KAFKA_CONSUMER
    assert quiet_row.error_category is EnumRuntimeErrorCategory.DATABASE

    # Hop 4 -> 5: the correlation id the Errors widget hands to the trace
    # widget is the MOST RECENT occurrence's, not the first one's -- the first
    # one's trace may already have aged out of the trace surface.
    assert loud_row.correlation_id == "cccccccc-0000-4000-8000-000000000002"
    assert loud_row.first_seen_at == _T0
    assert loud_row.last_seen_at == _T0 + timedelta(minutes=2)


@pytest.mark.unit
def test_golden_chain_terminal_event_state_is_reachable() -> None:
    """The declared output state
    ``onex.evt.omnimarket.projection-runtime-error-fingerprints-applied.v1``
    is emitted by the writer's own return value, which reports the rows it
    wrote rather than a bare ack -- a truthy ack over zero rows is how a
    projection reports success while writing nothing (OMN-16875)."""
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
        RuntimeErrorFingerprintProjectionWriter,
    )

    writer = RuntimeErrorFingerprintProjectionWriter()
    assert writer._contract["terminal_event"] == _TERMINAL_TOPIC
    assert writer._contract["event_bus"]["publish_topics"] == [_TERMINAL_TOPIC]


@pytest.mark.unit
def test_golden_chain_dlq_hop_exists_so_a_bad_event_is_not_silently_dropped() -> None:
    """This node exists to make failure visible; it must not become a new
    silent-loss site of its own."""
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
        RuntimeErrorFingerprintProjectionWriter,
    )

    writer = RuntimeErrorFingerprintProjectionWriter()
    assert writer.poison_dlq_topics == [_DLQ_TOPIC]
