"""Parity tests for the node_gap_compute topic probe regex.

The gap-compute probe (`_TOPIC_RE`) must stay in lockstep with the canonical
ONEX bus pattern (`omnibase_core.topics._CANONICAL_TOPIC_PATTERN`). When the
probe was stricter than canonical it rejected valid ``onex.snapshot.*`` and
``onex.dlq.*`` topics, emitting CRITICAL ``topic_name_mismatch`` false positives
for every snapshot/dlq topic declared in a contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.topics import _CANONICAL_TOPIC_PATTERN

from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    _TOPIC_RE,
    HandlerGapCompute,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    EnumGapStatus,
)

# Representative corpus: one entry per canonical kind (including the previously
# rejected dlq/snapshot kinds), plus malformed topics that both patterns reject.
_CANONICAL_TOPICS = [
    "onex.cmd.omniintelligence.claude-hook-event.v1",
    "onex.evt.omniclaude.session-started.v1",
    "onex.dlq.omniclaude.agent-observability-dlq.v1",
    "onex.snapshot.projection.cost.summary.v1",
    "onex.snapshot.projection.cost.by_repo.v1",
    "onex.intent.omnimarket.do-thing.v1",
    "onex.evt.omnibase-infra.savings-estimated.v1",
    "onex.cmd.omnibase-infra.delegation-inference-request.v1",
]

_NON_CANONICAL_TOPICS = [
    "not-a-topic",
    "onex.bogus.omnimarket.thing.v1",
    "onex.evt.omnimarket.no-version",
    "kafka.evt.omnimarket.thing.v1",
    "onex.evt.omnimarket.thing.vX",
]


@pytest.mark.unit
@pytest.mark.parametrize("topic", _CANONICAL_TOPICS)
def test_probe_accepts_every_canonical_topic(topic: str) -> None:
    assert _CANONICAL_TOPIC_PATTERN.match(topic), (
        f"corpus topic {topic} must be canonical for this parity test to be valid"
    )
    assert _TOPIC_RE.match(topic), (
        f"probe regex rejected canonical topic {topic}; it drifted stricter than "
        "omnibase_core.topics._CANONICAL_TOPIC_PATTERN"
    )


@pytest.mark.unit
@pytest.mark.parametrize("topic", _NON_CANONICAL_TOPICS)
def test_probe_rejects_non_canonical_topic(topic: str) -> None:
    assert not _CANONICAL_TOPIC_PATTERN.match(topic)
    assert not _TOPIC_RE.match(topic)


@pytest.mark.unit
@pytest.mark.parametrize("topic", _CANONICAL_TOPICS + _NON_CANONICAL_TOPICS)
def test_probe_verdict_matches_canonical_pattern(topic: str) -> None:
    """The probe must agree with canonical on accept/reject for every topic."""
    assert bool(_TOPIC_RE.match(topic)) == bool(_CANONICAL_TOPIC_PATTERN.match(topic))


@pytest.mark.unit
def test_probe_pattern_kinds_match_canonical_kinds() -> None:
    """Both patterns must whitelist the same set of topic kinds."""
    expected_kinds = "(cmd|evt|dlq|snapshot|intent)"
    assert expected_kinds in _TOPIC_RE.pattern
    assert expected_kinds in _CANONICAL_TOPIC_PATTERN.pattern


def _write_contract(path: Path, *, topic: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "node_sample",
        "node_type": "compute",
        "terminal_event": topic,
        "event_bus": {
            "subscribe_topics": ["onex.cmd.omnimarket.sample-start.v1"],
            "publish_topics": [topic],
        },
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize(
    "topic",
    [
        "onex.snapshot.projection.cost.summary.v1",
        "onex.dlq.omniclaude.agent-observability-dlq.v1",
    ],
)
def test_gap_detect_no_false_positive_for_snapshot_and_dlq(
    tmp_path: Path, topic: str
) -> None:
    """Regression: snapshot/dlq topics must not produce topic_name_mismatch."""
    repo = tmp_path / "omnimarket"
    _write_contract(
        repo / "src/omnimarket/nodes/node_sample/contract.yaml",
        topic=topic,
    )

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(repo_roots=[str(repo)], dry_run=True)
    )

    assert result.status == EnumGapStatus.CLEAN
    assert result.findings == []
