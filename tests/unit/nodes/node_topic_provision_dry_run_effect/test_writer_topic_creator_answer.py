# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The creator reads the broker's per-topic answer: a refusal is never reported as created."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiokafka.errors import TopicAuthorizationFailedError

from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters import (
    broker_topics,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.cmd.example.work-ledger-append-from-lane-a.v1"


class FakeAdmin:
    """An admin client whose create answer is one (topic, error_code, message) row."""

    def __init__(self, error_code: int, message: str | None) -> None:
        self._row = (TOPIC, error_code, message)
        self.requested: list[tuple[str, int, int]] = []

    async def create_topics(self, new_topics: list[Any]) -> SimpleNamespace:
        for topic in new_topics:
            self.requested.append(
                (topic.name, topic.num_partitions, topic.replication_factor)
            )
        return SimpleNamespace(topic_errors=[self._row])


def _use(monkeypatch: pytest.MonkeyPatch, admin: FakeAdmin) -> None:
    @asynccontextmanager
    async def fake_client(_bootstrap: str) -> AsyncIterator[FakeAdmin]:
        yield admin

    monkeypatch.setattr(broker_topics, "_admin_client", fake_client)


async def test_writer_topic_creator_reports_created_on_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = FakeAdmin(0, None)
    _use(monkeypatch, admin)

    created = await broker_topics.AiokafkaBrokerTopicCreator(
        "broker:9092"
    ).create_topic(TOPIC, 1, 1)

    assert created is True
    assert admin.requested == [(TOPIC, 1, 1)]


async def test_writer_topic_creator_accepts_an_existing_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeAdmin(36, "Topic already exists"))

    created = await broker_topics.AiokafkaBrokerTopicCreator(
        "broker:9092"
    ).create_topic(TOPIC, 1, 1)

    assert created is False


async def test_writer_topic_creator_raises_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, FakeAdmin(29, "Unauthorized"))

    with pytest.raises(TopicAuthorizationFailedError, match="Unauthorized"):
        await broker_topics.AiokafkaBrokerTopicCreator("broker:9092").create_topic(
            TOPIC, 1, 1
        )
