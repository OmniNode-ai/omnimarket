# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unreachable Kafka identities back off without losing spooled events (OMN-17427)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiokafka.errors import TopicAuthorizationFailedError

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from tests.unit.nodes.node_event_emit_effect.test_handler_event_emit_effect import (
    FakePublishAdapter,
)

pytestmark = pytest.mark.unit


class IdentityPublishAdapter:
    """Count every attempt, including failures, without a Kafka connection."""

    def __init__(
        self, identity: str = "broker:9092|PLAINTEXT|", *, fail: bool = True
    ) -> None:
        self.publish_identity = identity
        self.calls = 0
        self.fail = fail
        self.fail_on_call: int | None = None
        self.refuse = False

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.calls += 1
        if self.refuse:
            raise TopicAuthorizationFailedError(topic)
        if self.fail or self.calls == self.fail_on_call:
            raise ConnectionError("broker unreachable")


@pytest.fixture
def spool(tmp_path: Path) -> SpoolOutbox:
    return SpoolOutbox(tmp_path / "spool")


@pytest.fixture
def request_model() -> ModelEmitRequest:
    return ModelEmitRequest(
        event_type="session.started",
        payload={"session_id": "abc"},
        correlation_id="6c580c03-eeb7-4a52-8db2-5f1d5c3258ec",
    )


def marker_path(spool: SpoolOutbox, adapter: IdentityPublishAdapter) -> Path:
    digest = hashlib.sha256(adapter.publish_identity.encode("utf-8")).hexdigest()
    return spool.spool_dir / "publish-backoff" / f"{digest[:16]}.json"


def age_marker(marker: Path, seconds: float) -> None:
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["failed_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    marker.write_text(json.dumps(data), encoding="utf-8")


def test_failure_backs_off_across_handler_instances(
    spool: SpoolOutbox,
    request_model: ModelEmitRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = IdentityPublishAdapter()
    first = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        request_model
    )

    marker = marker_path(spool, adapter)
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert (
        data["identity_sha256"]
        == hashlib.sha256(adapter.publish_identity.encode("utf-8")).hexdigest()
    )
    assert datetime.fromisoformat(data["failed_at"]).utcoffset() == timedelta(0)
    assert adapter.calls == 1
    assert first.published is False
    assert first.publish_skipped_unreachable is False

    next_adapter = IdentityPublishAdapter()
    with caplog.at_level(logging.INFO):
        second = HandlerEventEmitEffect(
            spool=spool, publish_adapter=next_adapter
        ).handle(request_model.model_copy(update={"event_id": "second"}))

    assert next_adapter.calls == 0
    assert second.publish_skipped_unreachable is True
    assert second.published is False
    assert second.spool_only is False
    assert second.drained_count == 0
    assert second.correlation_id == first.correlation_id
    assert len(spool.list_pending()) == 2
    assert "stays spooled for the next drain" in caplog.text


def test_expired_marker_allows_publish_and_backlog_drain(
    spool: SpoolOutbox, request_model: ModelEmitRequest
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=adapter, unreachable_backoff_seconds=0.5
    )
    handler.handle(request_model)
    marker = marker_path(spool, adapter)
    age_marker(marker, 10)
    adapter.fail = False

    result = handler.handle(request_model)

    assert result.published is True
    assert result.publish_skipped_unreachable is False
    assert result.drained_count == 1
    assert adapter.calls == 3
    assert spool.list_pending() == []
    assert not marker.exists()


def test_other_identity_is_not_backed_off(
    spool: SpoolOutbox, request_model: ModelEmitRequest
) -> None:
    failing = IdentityPublishAdapter()
    HandlerEventEmitEffect(spool=spool, publish_adapter=failing).handle(request_model)
    other = IdentityPublishAdapter("broker:9092|SASL_PLAINTEXT|u", fail=False)

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=other).handle(
        request_model
    )

    assert result.published is True
    assert result.drained_count == 1
    assert other.calls == 2
    assert marker_path(spool, failing).exists()


def test_adapter_without_identity_never_backs_off(
    spool: SpoolOutbox, request_model: ModelEmitRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakePublishAdapter()
    attempts: list[str] = []

    def fail(topic: str, *args: object, **kwargs: object) -> None:
        attempts.append(topic)
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(adapter, "publish", fail)
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    for _ in range(2):
        assert handler.handle(request_model).publish_skipped_unreachable is False

    assert len(attempts) == 2
    assert len(spool.list_pending()) == 2
    assert not (spool.spool_dir / "publish-backoff").exists()


def test_zero_disables_backoff_even_with_existing_marker(
    spool: SpoolOutbox, request_model: ModelEmitRequest
) -> None:
    adapter = IdentityPublishAdapter()
    HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(request_model)
    handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=adapter, unreachable_backoff_seconds=0
    )

    for _ in range(2):
        assert handler.handle(request_model).publish_skipped_unreachable is False

    assert adapter.calls == 3
    assert len(spool.list_pending()) == 3


def test_marker_is_never_pending(
    spool: SpoolOutbox, request_model: ModelEmitRequest
) -> None:
    adapter = IdentityPublishAdapter()
    HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(request_model)
    marker = marker_path(spool, adapter)

    assert marker.is_file()
    assert list(spool.spool_dir.glob("*.json")) == [spool.list_pending()[0].path]
    assert marker not in [record.path for record in spool.list_pending()]
    assert spool.pending_count() == 1


def test_default_adapter_identity_excludes_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_EMIT_EFFECT_SPOOL_ONLY")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "SASL_PLAINTEXT")
    monkeypatch.setenv("KAFKA_SASL_MECHANISM", "PLAIN")
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "u")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "secretpw")

    adapter = HandlerEventEmitEffect()._build_default_adapter()

    assert isinstance(adapter, KafkaEventPublisher)
    assert adapter.publish_identity == "broker:9092|SASL_PLAINTEXT|u"
    assert "secretpw" not in adapter.publish_identity


@pytest.mark.parametrize("age", [-300, 300])
def test_future_or_expired_marker_is_refreshed_on_failure(
    spool: SpoolOutbox, request_model: ModelEmitRequest, age: float
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    handler.handle(request_model)
    marker = marker_path(spool, adapter)
    age_marker(marker, age)

    result = handler.handle(request_model)

    assert result.publish_skipped_unreachable is False
    assert adapter.calls == 2
    failed_at = datetime.fromisoformat(json.loads(marker.read_text())["failed_at"])
    assert 0 <= (datetime.now(UTC) - failed_at).total_seconds() < 5


@pytest.mark.parametrize(
    "contents",
    ["{", "[]", "{}", "non-string", "invalid-date", "naive-date"],
)
def test_malformed_marker_does_not_block_publish(
    spool: SpoolOutbox, request_model: ModelEmitRequest, contents: str
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    handler.handle(request_model)
    marker = marker_path(spool, adapter)
    if contents in {"non-string", "invalid-date", "naive-date"}:
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["failed_at"] = {
            "non-string": 1,
            "invalid-date": "invalid",
            "naive-date": "2026-10-20T10:00:00",
        }[contents]
        contents = json.dumps(data)
    marker.write_text(contents, encoding="utf-8")

    assert handler.handle(request_model).publish_skipped_unreachable is False
    assert adapter.calls == 2


def test_backlog_failure_writes_marker(
    spool: SpoolOutbox, request_model: ModelEmitRequest
) -> None:
    HandlerEventEmitEffect(spool=spool).handle(request_model)
    adapter = IdentityPublishAdapter()

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        ModelEmitRequest(event_type="skill.friction_recorded", payload={})
    )

    assert result.published is False
    assert result.drained_count == 0
    assert adapter.calls == 1
    assert marker_path(spool, adapter).is_file()
    assert len(spool.list_pending()) == 1


@pytest.mark.parametrize("backlog_only", [False, True])
def test_any_success_clears_marker_even_if_later_publish_fails(
    spool: SpoolOutbox, request_model: ModelEmitRequest, backlog_only: bool
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    handler.handle(request_model)
    HandlerEventEmitEffect(spool=spool).handle(request_model)
    marker = marker_path(spool, adapter)
    age_marker(marker, 300)
    adapter.fail = False
    adapter.fail_on_call = 3

    result = handler.handle(
        ModelEmitRequest(event_type="skill.friction_recorded", payload={})
        if backlog_only
        else request_model
    )

    assert result.published is not backlog_only
    assert result.drained_count == (1 if backlog_only else 0)
    assert adapter.calls == 3
    assert not marker.exists()
    assert len(spool.list_pending()) == (1 if backlog_only else 2)


@pytest.mark.parametrize("existing_marker", [False, True])
def test_quarantine_does_not_change_marker(
    spool: SpoolOutbox, request_model: ModelEmitRequest, existing_marker: bool
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    marker = marker_path(spool, adapter)
    previous = None
    if existing_marker:
        handler.handle(request_model)
        age_marker(marker, 300)
        previous = marker.read_bytes()
    adapter.refuse = True

    result = handler.handle(request_model)

    assert result.quarantined_count == (2 if existing_marker else 1)
    assert (marker.read_bytes() if marker.exists() else None) == previous


@pytest.mark.parametrize("operation", ["read_text", "write_text", "unlink"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_marker_errors_do_not_prevent_publish(
    spool: SpoolOutbox,
    request_model: ModelEmitRequest,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    error_type: type[Exception],
) -> None:
    adapter = IdentityPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    handler.handle(request_model)
    marker = marker_path(spool, adapter)
    age_marker(marker, 300)
    original = getattr(Path, operation)

    def fail_marker(path: Path, *args: object, **kwargs: object) -> object:
        if path == marker:
            raise error_type("marker unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail_marker)
    adapter.fail = operation == "write_text"
    with caplog.at_level(logging.DEBUG):
        result = handler.handle(request_model)

    assert adapter.calls == (2 if adapter.fail else 3)
    assert result.publish_skipped_unreachable is False
    assert result.published is not adapter.fail
    assert "unreachable marker" in caplog.text
