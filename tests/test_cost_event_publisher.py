# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/cost_event_publisher.py.

Covers:
- Schema validation (valid and invalid files)
- Idempotency key computation
- File quarantine behavior (rejected/ + .error sidecar)
- Published file moved to published/
- Kafka publish failure → retry → quarantine
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.cost_event_publisher import (
    TOPIC,
    CostEventPublisher,
    compute_idempotency_key,
    compute_source_file_sha256,
    validate_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_spool(tmp_path: Path) -> Path:
    spool = tmp_path / "llm-cost-events"
    spool.mkdir()
    return spool


def _valid_payload(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "sess-abc123",
        "model_id": "qwen3-coder-30b",
        "reporting_source": "build-loop",
        "usage_source": "MEASURED",
        "correlation_id": str(uuid.uuid4()),
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_cost_usd": 0.0042,
    }
    if overrides:
        base.update(overrides)
    return base


def _write_event(
    spool: Path, payload: dict[str, Any], name: str = "event.json"
) -> Path:
    p = spool / name
    p.write_text(json.dumps(payload))
    return p


# ---------------------------------------------------------------------------
# compute_source_file_sha256
# ---------------------------------------------------------------------------


class TestComputeSourceFileSha256:
    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "ev.json"
        f.write_text('{"a": 1}')
        assert compute_source_file_sha256(f) == compute_source_file_sha256(f)

    def test_changes_with_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text('{"a": 1}')
        f2.write_text('{"a": 2}')
        assert compute_source_file_sha256(f1) != compute_source_file_sha256(f2)

    def test_matches_manual_sha256(self, tmp_path: Path) -> None:
        content = b'{"x": 42}'
        f = tmp_path / "ev.json"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert compute_source_file_sha256(f) == expected


# ---------------------------------------------------------------------------
# compute_idempotency_key
# ---------------------------------------------------------------------------


class TestComputeIdempotencyKey:
    def test_deterministic(self) -> None:
        key = compute_idempotency_key(
            reporting_source="build-loop",
            session_id="s1",
            correlation_id="c1",
            model_id="m1",
            source_file_sha256="sha1",
        )
        assert key == compute_idempotency_key(
            reporting_source="build-loop",
            session_id="s1",
            correlation_id="c1",
            model_id="m1",
            source_file_sha256="sha1",
        )

    def test_changes_with_each_field(self) -> None:
        base = {
            "reporting_source": "build-loop",
            "session_id": "s1",
            "correlation_id": "c1",
            "model_id": "m1",
            "source_file_sha256": "sha1",
        }
        key0 = compute_idempotency_key(**base)

        for field, value in [
            ("reporting_source", "codex"),
            ("session_id", "s2"),
            ("correlation_id", "c2"),
            ("model_id", "m2"),
            ("source_file_sha256", "sha2"),
        ]:
            changed = {**base, field: value}
            assert compute_idempotency_key(**changed) != key0, field

    def test_is_hex_sha256(self) -> None:
        key = compute_idempotency_key("a", "b", "c", "d", "e")
        assert len(key) == 64
        int(key, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# validate_event
# ---------------------------------------------------------------------------


class TestValidateEvent:
    def test_valid_payload_passes(self) -> None:
        errors = validate_event(_valid_payload())
        assert errors == []

    def test_missing_session_id(self) -> None:
        payload = _valid_payload()
        del payload["session_id"]
        errors = validate_event(payload)
        assert any("session_id" in e for e in errors)

    def test_missing_model_id(self) -> None:
        payload = _valid_payload()
        del payload["model_id"]
        errors = validate_event(payload)
        assert any("model_id" in e for e in errors)

    def test_invalid_usage_source(self) -> None:
        payload = _valid_payload({"usage_source": "WRONG"})
        errors = validate_event(payload)
        assert any("usage_source" in e for e in errors)

    def test_invalid_reporting_source(self) -> None:
        payload = _valid_payload({"reporting_source": "bad-source"})
        errors = validate_event(payload)
        assert any("reporting_source" in e for e in errors)

    def test_invalid_correlation_id(self) -> None:
        payload = _valid_payload({"correlation_id": "not-a-uuid"})
        errors = validate_event(payload)
        assert any("correlation_id" in e for e in errors)

    def test_all_valid_usage_sources(self) -> None:
        for source in ("MEASURED", "ESTIMATED", "UNKNOWN"):
            assert validate_event(_valid_payload({"usage_source": source})) == []

    def test_all_valid_reporting_sources(self) -> None:
        for source in ("build-loop", "claude-session", "codex", "unknown"):
            assert validate_event(_valid_payload({"reporting_source": source})) == []


# ---------------------------------------------------------------------------
# CostEventPublisher — quarantine behavior
# ---------------------------------------------------------------------------


class TestCostEventPublisherQuarantine:
    @pytest.mark.asyncio
    async def test_invalid_json_quarantined(self, tmp_spool: Path) -> None:
        bad = tmp_spool / "bad.json"
        bad.write_text("not json {{{")

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(bad)

        rejected_dir = tmp_spool / "rejected"
        assert not bad.exists()
        assert (rejected_dir / "bad.json").exists()
        assert (rejected_dir / "bad.json.error").exists()
        error_text = (rejected_dir / "bad.json.error").read_text()
        assert "JSON" in error_text or "json" in error_text.lower()
        mock_producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_field_quarantined(self, tmp_spool: Path) -> None:
        payload = _valid_payload()
        del payload["model_id"]
        f = _write_event(tmp_spool, payload)

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        rejected_dir = tmp_spool / "rejected"
        assert not f.exists()
        assert (rejected_dir / f.name).exists()
        error_text = (rejected_dir / f"{f.name}.error").read_text()
        assert "model_id" in error_text

    @pytest.mark.asyncio
    async def test_kafka_failure_exhausts_retries_and_quarantines(
        self, tmp_spool: Path
    ) -> None:
        f = _write_event(tmp_spool, _valid_payload())

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
            max_retries=3,
            retry_backoff_seconds=0.0,
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(
            side_effect=Exception("broker unavailable")
        )

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        rejected_dir = tmp_spool / "rejected"
        assert not f.exists()
        assert (rejected_dir / f.name).exists()
        assert mock_producer.send_and_wait.call_count == 3


# ---------------------------------------------------------------------------
# CostEventPublisher — happy path
# ---------------------------------------------------------------------------


class TestCostEventPublisherHappyPath:
    @pytest.mark.asyncio
    async def test_valid_file_published_and_moved(self, tmp_spool: Path) -> None:
        f = _write_event(tmp_spool, _valid_payload(), name="good.json")

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(return_value=MagicMock())

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        published_dir = tmp_spool / "published"
        assert not f.exists()
        assert (published_dir / "good.json").exists()
        mock_producer.send_and_wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_published_payload_contains_idempotency_key(
        self, tmp_spool: Path
    ) -> None:
        payload = _valid_payload()
        f = _write_event(tmp_spool, payload)

        published_value: bytes | None = None

        async def capture_send(
            topic: str, value: bytes, key: bytes | None = None
        ) -> None:
            nonlocal published_value
            published_value = value

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(side_effect=capture_send)

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        assert published_value is not None
        published = json.loads(published_value)
        assert "idempotency_key" in published
        assert len(published["idempotency_key"]) == 64
        assert "source_file_sha256" in published
        assert "emitted_at" in published

    @pytest.mark.asyncio
    async def test_published_to_correct_topic(self, tmp_spool: Path) -> None:
        f = _write_event(tmp_spool, _valid_payload())

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(return_value=MagicMock())

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        call_args = mock_producer.send_and_wait.call_args
        assert (
            call_args[0][0] == TOPIC
            or call_args[1].get("topic") == TOPIC
            or call_args[0][0] == TOPIC
        )

    @pytest.mark.asyncio
    async def test_message_key_is_idempotency_key(self, tmp_spool: Path) -> None:
        payload = _valid_payload()
        f = _write_event(tmp_spool, payload)

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )

        captured_key: bytes | None = None
        captured_value: bytes | None = None

        async def capture(topic: str, value: bytes, key: bytes | None = None) -> None:
            nonlocal captured_key, captured_value
            captured_key = key
            captured_value = value

        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(side_effect=capture)

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        assert captured_value is not None
        published = json.loads(captured_value)
        assert captured_key == published["idempotency_key"].encode()

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self, tmp_spool: Path) -> None:
        f = _write_event(tmp_spool, _valid_payload())

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
            max_retries=3,
            retry_backoff_seconds=0.0,
        )
        call_count = 0

        async def flaky_send(
            topic: str, value: bytes, key: bytes | None = None
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("transient error")

        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(side_effect=flaky_send)

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        published_dir = tmp_spool / "published"
        assert (published_dir / f.name).exists()
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_already_published_subdir_files_skipped(
        self, tmp_spool: Path
    ) -> None:
        published_dir = tmp_spool / "published"
        published_dir.mkdir()
        already = published_dir / "old.json"
        already.write_text(json.dumps(_valid_payload()))

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.poll_once()

        mock_producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_usage_source_added_when_missing(self, tmp_spool: Path) -> None:
        payload = _valid_payload()
        del payload["usage_source"]
        f = _write_event(tmp_spool, payload)

        captured_value: bytes | None = None

        async def capture(topic: str, value: bytes, key: bytes | None = None) -> None:
            nonlocal captured_value
            captured_value = value

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(side_effect=capture)

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            await publisher.process_file(f)

        # File with missing usage_source should be quarantined (it's required)
        rejected_dir = tmp_spool / "rejected"
        assert (rejected_dir / f.name).exists()


# ---------------------------------------------------------------------------
# poll_once: scans only top-level .json files
# ---------------------------------------------------------------------------


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_processes_multiple_files(self, tmp_spool: Path) -> None:
        for i in range(3):
            _write_event(tmp_spool, _valid_payload(), f"event_{i}.json")

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(return_value=MagicMock())

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            count = await publisher.poll_once()

        assert count == 3
        assert mock_producer.send_and_wait.call_count == 3

    @pytest.mark.asyncio
    async def test_non_json_files_ignored(self, tmp_spool: Path) -> None:
        (tmp_spool / "notes.txt").write_text("ignore me")
        (tmp_spool / "data.yaml").write_text("ignore: me")
        _write_event(tmp_spool, _valid_payload(), "real.json")

        publisher = CostEventPublisher(
            spool_dir=tmp_spool,
            bootstrap_servers="localhost:9092",
        )
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock(return_value=MagicMock())

        with patch(
            "scripts.cost_event_publisher.AIOKafkaProducer", return_value=mock_producer
        ):
            count = await publisher.poll_once()

        assert count == 1
