# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The renderer-capability projection must own its DLQ, not borrow the platform's.

OMN-17497. ``node_renderer_capability_projection`` declared no
``event_bus.dlq_topics``, so every erroring event fell through to the
platform-wide quarantine sink ``onex.dlq.omnibase-infra.quarantine.v1`` --
shared by every handler on the runtime. Two consequences, both live on
``onex-dev`` on 2026-09-01:

* **Unfindable.** The quarantine sink held 8,878,926 retained records as of
  OMN-16769. A per-node failure signal deposited there is not triageable.
* **Blast radius.** The Kafka circuit breaker is keyed on the CONNECTION
  (``kafka.onex-dev``). This node's quarantine publishes were failing
  ``OutOfOrderSequenceNumber``, which opened the shared breaker 108 times in
  two hours and intermittently broke the gateway attach/heartbeat/detach path
  on the same pod.

The infra-side cut (a failure-path publish never charges the shared breaker)
landed in omnibase_infra under the same ticket. This is the node-side cut: the
node names its own sink, so its failures stay attributable to it.

Related Tickets:
    - OMN-17497: this ticket
    - OMN-16690: the ``access='write'`` vs read defect that produced the
      poison event in the first place (already fixed on omnimarket dev)
    - OMN-13548: the projection DLQ routing contract this declaration feeds
    - OMN-16769: the 8.88M-record quarantine sink backlog
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_renderer_capability_projection"
    / "contract.yaml"
)

_EXPECTED_DLQ_TOPIC = "onex.dlq.omnimarket.projection-renderer-capability-malformed.v1"
_PLATFORM_QUARANTINE_SINK = "onex.dlq.omnibase-infra.quarantine.v1"


@pytest.fixture(scope="module")
def contract() -> dict[str, object]:
    with _CONTRACT_PATH.open() as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
class TestRendererCapabilityProjectionOwnsItsDlq:
    def test_declares_a_dlq_topic(self, contract: dict[str, object]) -> None:
        """Without this the runtime silently borrows the shared quarantine sink."""
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        dlq_topics = event_bus.get("dlq_topics")
        assert dlq_topics, (
            "node_renderer_capability_projection must declare "
            "event_bus.dlq_topics; with none declared the auto-wiring falls "
            "back to the platform quarantine sink shared by every handler on "
            "the runtime (OMN-17497)"
        )
        assert _EXPECTED_DLQ_TOPIC in dlq_topics

    def test_dlq_is_node_owned_not_the_platform_quarantine_sink(
        self, contract: dict[str, object]
    ) -> None:
        """Naming the shared sink explicitly would reintroduce the same coupling."""
        event_bus = contract["event_bus"]
        assert isinstance(event_bus, dict)
        dlq_topics = event_bus.get("dlq_topics") or []
        assert _PLATFORM_QUARANTINE_SINK not in dlq_topics, (
            "the platform quarantine sink is the FALLBACK for nodes that "
            "declare nothing; declaring it explicitly keeps this node's "
            "failures in an 8.88M-record shared bucket (OMN-16769)"
        )
        for topic in dlq_topics:
            assert str(topic).startswith("onex.dlq.omnimarket."), (
                f"{topic} is not an omnimarket-owned DLQ sink"
            )

    def test_declared_table_access_covers_the_read_the_handler_performs(
        self, contract: dict[str, object]
    ) -> None:
        """Regression guard for the OMN-16690 defect that produced the poison event.

        The handler queries the table before writing. A ``write``-only
        declaration is refused fail-closed at the runtime read seam
        (``ProjectionTableOperation._assert_read_declared``), which sends every
        single event to the DLQ while the caller still sees a 202. Adopted from
        OMN-16690, asserted here so the two halves of OMN-17497's chain cannot
        drift apart.
        """
        db_io = contract["db_io"]
        assert isinstance(db_io, dict)
        tables = db_io["db_tables"]
        assert isinstance(tables, list)
        table = next(
            t
            for t in tables
            if isinstance(t, dict) and t.get("name") == "renderer_capability_projection"
        )
        assert table["access"] == "read_write", (
            "the handler reads before writing; a narrower declaration is "
            "refused at the read seam and DLQs every event (OMN-16690)"
        )
