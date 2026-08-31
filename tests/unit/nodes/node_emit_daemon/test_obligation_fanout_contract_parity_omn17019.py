# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_emit_daemon must declare the work-obligation fan-out it produces (OMN-17019).

Two surfaces have to agree for an obligation event to reach its projection:

* ``registries/topics.yaml`` -- what the daemon actually fans an event type out
  to at runtime;
* ``node_emit_daemon/contract.yaml -> event_bus.publish_topics`` -- what the
  contract graph believes the daemon produces.

They are not the same file and nothing regenerates one from the other, so they
drift silently. The failure mode is specific and was live while this ticket was
being built: with the registry entries added and the contract not updated, the
contract-topic-graph gate classifies node_projection_open_obligations as an
ORPHANED_CONSUMER -- "subscribes to a topic that NO contract publishes ...
nothing can ever send it a message". The projection would have been merged
wired to five topics with no declared producer.

Scoped to the five obligation kinds deliberately. A whole-registry parity
assertion would also fire on ``onex.evt.diagnostic.daemon-health.v1``, which is
excluded from the contract on purpose (non-canonical service segment, allowlisted
in ``dep_health_allowlist.yaml``) -- so a broad version of this test would have
to carry an exception list, and an exception list is how the next drift gets
waved through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationEventKind,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMIT_DAEMON_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_emit_daemon"
_REGISTRY = _EMIT_DAEMON_DIR / "registries" / "topics.yaml"
_CONTRACT = _EMIT_DAEMON_DIR / "contract.yaml"

# The five wire topics, written out so this file names the states the
# contract-state-coverage gate requires a test to assert.
OBLIGATION_TOPICS: tuple[str, ...] = (
    "onex.evt.omniclaude.work-obligation-created.v1",
    "onex.evt.omniclaude.work-obligation-transferred.v1",
    "onex.evt.omniclaude.work-obligation-satisfied.v1",
    "onex.evt.omniclaude.work-obligation-superseded.v1",
    "onex.evt.omniclaude.work-obligation-abandoned.v1",
)


def test_registry_fans_each_obligation_kind_out_to_exactly_one_topic() -> None:
    events = yaml.safe_load(_REGISTRY.read_text())["events"]
    fanned = {
        topic
        for kind in EnumObligationEventKind
        for topic in (fan["topic"] for fan in events[kind.value]["fan_out"])
    }
    assert fanned == set(OBLIGATION_TOPICS)


def test_emit_daemon_contract_declares_every_obligation_fanout_topic() -> None:
    """Without this, the projection is an ORPHANED_CONSUMER on all five."""
    published = set(
        yaml.safe_load(_CONTRACT.read_text())["event_bus"]["publish_topics"]
    )
    missing = [topic for topic in OBLIGATION_TOPICS if topic not in published]
    assert not missing, (
        "node_emit_daemon/contract.yaml does not declare "
        f"{missing} -- the contract graph will report the open-obligations "
        "projection as an orphaned consumer of them"
    )
