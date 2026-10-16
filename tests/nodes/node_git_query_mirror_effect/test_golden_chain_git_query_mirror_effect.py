# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scratch git -> canonical mirror node -> durable spool -> adapter -> bus.

The production Kafka adapter runs against the real in-memory event bus. Its
failure seam covers recovery without replacing the mirror, spool, serialization
or adapter. These tests make no live-broker delivery claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import ModelEventHeaders, ModelPublishReceipt

from omnimarket.events.github import ModelPrMergedEvent, pr_merged_event_id
from omnimarket.nodes.node_event_emit_effect import KafkaEventPublisher
from omnimarket.nodes.node_git_query_mirror_effect import NodeGitQueryMirrorEffect
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    ModelGitQueryRequest,
    ModelGitQueryResponse,
)
from tests.nodes.node_git_query_mirror_effect.test_omn19834_mirror_merge_events import (
    CONTRACT,
    REPO,
    TripwireGh,
    Upstream,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("fail_first", [False, True], ids=["publish", "restart"])
async def test_git_merge_outbox_to_bus_and_projection_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_first: bool
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    upstream = Upstream(tmp_path)
    gh = TripwireGh()
    topic = yaml.safe_load(CONTRACT.read_text())["event_bus"]["publish_topics"][0]
    number = 19998

    class ControlledBus(EventBusInmemory):
        fail = fail_first

        async def publish(
            self,
            topic: str,
            key: bytes | None,
            value: bytes,
            headers: ModelEventHeaders | None = None,
        ) -> ModelPublishReceipt:
            # Re-enter the persisted mirror while the real adapter is publishing:
            # the marker must still be absent on both sides of bus acceptance.
            assert not node().mirror().is_merge_published(REPO, number)
            if self.fail:
                raise ConnectionError("golden-chain broker outage")
            receipt = await super().publish(topic, key, value, headers)
            assert not node().mirror().is_merge_published(REPO, number)
            return receipt

    bus = ControlledBus()

    def node() -> NodeGitQueryMirrorEffect:
        return NodeGitQueryMirrorEffect(
            remote_url_for=lambda _repo: str(upstream.path),
            reference_clone_for=lambda _repo: None,
            gh_runner=gh,
            publish_adapter=KafkaEventPublisher(
                "unused:9092",
                source="node_git_query_mirror_effect",
                bus_factory=lambda: bus,
            ),
        )

    request = ModelGitQueryRequest(operation="sync", repo=REPO)
    baseline = await node().handle(request)
    assert baseline.events[0].results[0].ok
    assert await bus.get_event_history(topic=topic) == []

    head = upstream.open_pr(number, "omn-19998-golden", "golden.txt")
    merge = upstream.squash_merge("omn-19998-golden", f"golden (#{number})")
    original_payload: dict[str, object] | None = None
    if fail_first:
        with pytest.raises(ConnectionError, match="golden-chain broker outage"):
            await node().handle(request)
        mirror = node().mirror()
        (pending,) = mirror.pending_merges(REPO)
        original_payload = pending.model_dump(mode="json")
        assert not mirror.is_merge_published(REPO, number)
        assert await bus.get_event_history(topic=topic) == []
        bus.fail = False

    # A fresh canonical node reconstructs all pending state from disk.
    restarted = node()
    output = await restarted.handle(request)
    (response,) = output.events
    assert isinstance(response, ModelGitQueryResponse)
    assert response.results[0].ok
    (message,) = await bus.get_event_history(topic=topic)
    payload = json.loads(message.value)
    projected = ModelPrMergedEvent.model_validate(payload)
    assert message.topic == topic
    assert message.key == REPO.encode()
    assert projected.event_id == str(pr_merged_event_id(REPO, number, merge))
    assert projected.repo == REPO
    assert projected.pr_number == number
    assert payload["head_sha"] == head
    assert payload["source"] == "mirror"
    if original_payload is not None:
        assert payload == original_payload
    assert restarted.mirror().is_merge_published(REPO, number)
    assert restarted.mirror().pending_merges(REPO) == []

    await node().handle(request)
    assert len(await bus.get_event_history(topic=topic)) == 1
    assert gh.calls == []
