# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_topic_archive_replay_effect: verify, refuse, replay with provenance."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    HandlerTopicArchive,
)
from omnimarket.nodes.node_topic_archive_replay_effect.handlers.handler_topic_archive_replay import (
    HandlerTopicArchiveReplay,
)
from omnimarket.topic_archive.models import (
    ModelTopicArchiveReplayRequest,
    ModelTopicArchiveRequest,
)
from tests.topic_archive_fakes import (
    DAY1,
    NODES,
    MemorySink,
    MemoryWriter,
    T,
    XorCipher,
    ms,
    source,
)

pytestmark = pytest.mark.unit

# --- replay ----------------------------------------------------------------


async def _archived() -> MemorySink:
    sink = MemorySink()
    await HandlerTopicArchive(reader=source(), sink=sink, cipher=XorCipher()).handle(
        ModelTopicArchiveRequest(topics=[T])
    )
    return sink


def _replay_topic() -> str:
    contract = yaml.safe_load(
        (NODES / "node_topic_archive_replay_effect" / "contract.yaml").read_text()
    )
    topic: str = contract["config"]["topic_archive_replay"]["replay_topic"]
    return topic


async def test_replay_puts_every_record_on_the_declared_replay_topic_with_provenance() -> (
    None
):
    sink = await _archived()
    writer = MemoryWriter()
    result = await HandlerTopicArchiveReplay(
        sink=sink, cipher=XorCipher(), writer=writer
    ).handle(ModelTopicArchiveReplayRequest(manifest_prefix=f"{T}/"))
    assert result.replayed_records == 3
    assert result.verified_files == 2
    assert _replay_topic() == "onex.evt.omnimarket.topic-archive-replay.v1"
    assert {s[0] for s in writer.sent} == {_replay_topic()}
    _topic, key, value, headers, ts = writer.sent[0]
    assert (key, value, ts) == (b"k", b'{"a": 1}', ms(DAY1))
    h = dict(headers)
    assert h["onex-archive-source-topic"] == T.encode()
    assert h["onex-archive-source-offset"] == b"10"
    assert h["correlation_id"] == b"c-1"


async def test_replay_refuses_a_file_whose_checksum_does_not_match() -> None:
    sink = await _archived()
    name = next(n for n in sink.objects if n.endswith(".age"))
    sink.objects[name] = sink.objects[name] + b"!"
    writer = MemoryWriter()
    result = await HandlerTopicArchiveReplay(
        sink=sink, cipher=XorCipher(), writer=writer
    ).handle(ModelTopicArchiveReplayRequest(manifest_prefix=f"{T}/"))
    assert result.refused_files == 1
    assert result.replayed_records == 1  # only the intact day


async def test_replay_never_targets_the_source_topic() -> None:
    sink = await _archived()
    writer = MemoryWriter()
    result = await HandlerTopicArchiveReplay(
        sink=sink, cipher=XorCipher(), writer=writer
    ).handle(ModelTopicArchiveReplayRequest(manifest_prefix=f"{T}/", replay_topic=T))
    assert writer.sent == []
    assert result.refused_files == 2


async def test_replay_dry_run_publishes_nothing() -> None:
    sink = await _archived()
    writer = MemoryWriter()
    result = await HandlerTopicArchiveReplay(
        sink=sink, cipher=XorCipher(), writer=writer
    ).handle(ModelTopicArchiveReplayRequest(manifest_prefix=f"{T}/", dry_run=True))
    assert writer.sent == []
    assert result.replayed_records == 0
    assert result.verified_files == 2
