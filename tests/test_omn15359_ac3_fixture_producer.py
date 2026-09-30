# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Falsifiers for the sanctioned OMN-15359 AC3 fixture publisher.

These tests are intentionally at the command boundary: a wrong lane, a mutable
topic, a missing registry mapping, or a duplicate correlation must fail before
the Kafka producer starts.  The positive case inspects the actual serialized
``ModelEventEnvelope`` handed to the producer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.projection.ac3_replay import ReplayError
from scripts import verify_omn15359_ac3_replay as command

_EXPECTED = {
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {
        "15359000-0000-4000-8000-000000000001",
        "15359000-0000-4000-8000-000000000002",
    },
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": {"15359000-0000-4000-8000-000000000003"},
}


@pytest.mark.parametrize(
    ("lane", "environment", "marker", "endpoint", "reason"),
    [
        ("operator-201", "lakshman", "", "redpanda:9092", "lane identity"),
        ("operator-201", "dev", "operator-201", "redpanda:9092", "environment"),
        ("shared-dev-201", "dev", "operator-201", "redpanda:9092", "lane identity"),
        ("shared-dev-201", "dev", "shared-dev-201", "prod-kafka:9092", "production"),
    ],
)
def test_lane_identity_refuses_ambiguous_or_production_targets(
    lane: str,
    environment: str,
    marker: str,
    endpoint: str,
    reason: str,
) -> None:
    with pytest.raises(ReplayError, match=reason):
        command._require_lane_identity(
            lane,
            {
                "AC3_LANE_IDENTITY": marker,
                "ONEX_ENVIRONMENT": environment,
                "KAFKA_BOOTSTRAP_SERVERS": endpoint,
            },
        )


def test_fixture_file_refuses_duplicate_correlation_ids(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures.json"
    duplicate = "15359000-0000-4000-8000-000000000001"
    fixture.write_text(
        json.dumps(
            {
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": [duplicate],
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": [duplicate],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match="multiple tenants"):
        command._fixtures(fixture)


def test_fixture_events_refuse_a_mutated_contract_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command,
        "contract_replay_topics",
        lambda _path: ("onex.evt.wrong.completed.v1", "onex.evt.wrong.failed.v1"),
    )
    with pytest.raises(ReplayError, match="canonical terminal topics"):
        command._fixture_events(_EXPECTED)


@pytest.mark.asyncio
async def test_missing_registry_mapping_refuses_before_producer_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = False

    class _Producer:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    def missing(*_args: object, **_kwargs: object) -> object:
        raise ReplayError("tenant registry mapping is missing")

    monkeypatch.setattr(command, "_require_registry_mappings", missing)
    monkeypatch.setattr(command, "AIOKafkaProducer", _Producer)
    monkeypatch.setenv("AC3_LANE_IDENTITY", "operator-201")
    monkeypatch.setenv("ONEX_ENVIRONMENT", "lakshman")
    monkeypatch.setenv("AC3_SOURCE_DSN", "postgresql://fixture")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
    with pytest.raises(ReplayError, match="registry mapping is missing"):
        await command._publish_fixture(
            lane="operator-201",
            expected=_EXPECTED,
            receipt_path=tmp_path / "receipt.json",
        )
    assert not constructed


@pytest.mark.asyncio
async def test_fixture_publish_uses_canonical_envelopes_and_both_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, object], bytes | None]] = []
    lifecycle: list[str] = []

    class _Producer:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["bootstrap_servers"] == "redpanda:9092"
            assert "value_serializer" in kwargs

        async def start(self) -> None:
            lifecycle.append("start")

        async def send_and_wait(
            self, topic: str, value: dict[str, object], *, key: bytes | None = None
        ) -> object:
            sent.append((topic, value, key))
            return SimpleNamespace(topic=topic, partition=0, offset=len(sent) + 10)

        async def stop(self) -> None:
            lifecycle.append("stop")

    monkeypatch.setattr(command, "_require_registry_mappings", lambda *_args: None)
    monkeypatch.setattr(command, "AIOKafkaProducer", _Producer)
    monkeypatch.setenv("AC3_LANE_IDENTITY", "operator-201")
    monkeypatch.setenv("ONEX_ENVIRONMENT", "lakshman")
    monkeypatch.setenv("AC3_SOURCE_DSN", "postgresql://fixture")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
    receipt_path = tmp_path / "fixture-receipt.json"

    receipt = await command._publish_fixture(
        lane="operator-201",
        expected=_EXPECTED,
        receipt_path=receipt_path,
    )

    assert lifecycle == ["start", "stop"]
    assert len(sent) == 3
    assert {topic for topic, _value, _key in sent} == {
        "onex.evt.omnibase-infra.delegation-completed.v1",
        "onex.evt.omnibase-infra.delegation-failed.v1",
    }
    for topic, envelope, key in sent:
        tenant = envelope["tenant_id"]
        payload = envelope["payload"]
        assert isinstance(tenant, str)
        assert isinstance(payload, dict)
        assert payload["tenant_id"] == tenant
        assert envelope["event_type"] == topic
        assert key == str(payload["correlation_id"]).encode("utf-8")
    assert receipt["published_records"] == 3
    assert len(receipt["events"]) == 3
    assert receipt_path.read_text(encoding="utf-8").endswith("\n")
