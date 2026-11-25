# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator entry builds its handler from the lane the settings or the flags name and prints one JSON line."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from omnimarket.nodes.node_tenant_topic_provision_effect import (
    runtime_tenant_topic_provision as entry,
)
from tests.nodes.node_tenant_topic_provision_effect.fakes import (
    FakeBroker,
    FakeMirrorSource,
    wire,
)

pytestmark = pytest.mark.unit


class _Selection:
    bootstrap_servers = "broker.invalid:9092"


@contextmanager
def _binding() -> Iterator[None]:
    yield


def _wire_entry(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    real_handler = entry.HandlerTenantTopicProvision
    monkeypatch.setattr(
        entry, "resolve_lane_binding", lambda _lane: (_Selection(), object())
    )
    monkeypatch.setattr(
        entry, "bind_lane_client_transport", lambda _transport: _binding()
    )
    monkeypatch.setattr(entry, "AiokafkaBrokerTopicLister", lambda _bootstrap: broker)
    monkeypatch.setattr(entry, "AiokafkaBrokerTopicCreator", lambda _bootstrap: broker)

    def build(**kwargs: Any) -> Any:
        return real_handler(mirror_source=FakeMirrorSource(), **kwargs)

    monkeypatch.setattr(entry, "HandlerTenantTopicProvision", build)


def test_entry_provisions_one_tenant_and_prints_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    _wire_entry(monkeypatch, broker)
    code = entry.main(
        [
            "--tenant",
            "acme",
            "--lane",
            "synthetic",
            "--partitions",
            "1",
            "--replicas",
            "1",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["outcome"] == "provisioned"
    assert sorted(broker.live) == sorted(wire("acme", _names()))


def test_entry_exits_one_and_names_the_topic_when_a_create_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = wire("acme", _names())[0]
    _wire_entry(monkeypatch, FakeBroker(refuse=frozenset({bad})))
    code = entry.main(
        [
            "--tenant",
            "acme",
            "--lane",
            "synthetic",
            "--partitions",
            "1",
            "--replicas",
            "1",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["outcome"] == "failed_topics"
    assert bad in out["detail"]


def _names() -> tuple[str, ...]:
    return FakeMirrorSource().mirror_topic_names()


def test_ac5_entry_reports_the_topics_a_consumer_saw(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    _wire_entry(monkeypatch, broker)
    asked: list[tuple[str, ...]] = []

    async def consumer(bootstrap: str, topics: Any) -> tuple[str, ...]:
        asked.append(tuple(topics))
        return tuple(topics)

    monkeypatch.setattr(entry, "consumer_visible_topics", consumer)
    code = entry.main(
        [
            "--tenant",
            "acme",
            "--lane",
            "synthetic",
            "--partitions",
            "1",
            "--replicas",
            "1",
            "--consumer-read-back",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    expected = sorted(wire("acme", _names()))
    assert code == 0
    assert sorted(asked[0]) == expected
    assert sorted(out["consumer_saw"]) == expected


def test_ac5_entry_reports_a_topic_no_consumer_saw(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire_entry(monkeypatch, FakeBroker())

    async def consumer(bootstrap: str, topics: Any) -> tuple[str, ...]:
        return tuple(sorted(topics))[1:]

    monkeypatch.setattr(entry, "consumer_visible_topics", consumer)
    entry.main(
        [
            "--tenant",
            "acme",
            "--lane",
            "synthetic",
            "--partitions",
            "1",
            "--replicas",
            "1",
            "--consumer-read-back",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert len(out["consumer_saw"]) == len(_names()) - 1
