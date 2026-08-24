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
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from scripts.cost_event_publisher import (
    TOPIC,
    CostEventPublisher,
    build_envelope_bytes,
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
# build_envelope_bytes — OMN-16417 gateway-forwarder outbound quarantine fix
#
# The gateway forwarder (omnibase_infra node_bus_forwarder_effect) decodes
# every outbound record as ModelEventEnvelope[dict[str, object]] before it
# will wrap and mirror it to cloud. Pre-fix, this daemon published the bare
# enriched dict directly, which has no `payload` field (required, no
# default) — every record failed ModelEventEnvelope.model_validate_json and
# was quarantined. This section proves both halves: (1) the field set below
# is the REAL on-wire schema observed via a read-only probe of a live
# quarantined record on .201 (keys only pulled, no payload values — see
# OMN-16417), so the golden case is not a fabricated shape; (2) the fixed
# wire bytes now decode cleanly through the exact model the forwarder uses.
# ---------------------------------------------------------------------------


# Field set matches the live schema pulled from a real quarantined record on
# onex.evt.omniintelligence.llm-call-completed.v1 (.201 dev lane, 2026-08-23):
# {"correlation_id", "emitted_at", "idempotency_key", "input_tokens",
#  "model_id", "output_tokens", "reporting_source", "session_id",
#  "source_file_sha256", "total_cost_usd", "usage_source"} — only key names
# were read off the live bus; no field values were captured or reused here.
_GOLDEN_ENRICHED_PAYLOAD: dict[str, Any] = {
    "correlation_id": str(uuid.uuid4()),
    "emitted_at": "2026-08-23T14:00:00+00:00",
    "idempotency_key": "a" * 64,
    "input_tokens": 512,
    "model_id": "qwen3.8",
    "output_tokens": 128,
    "reporting_source": "build-loop",
    "session_id": "sess-golden",
    "source_file_sha256": "b" * 64,
    "total_cost_usd": 0.0031,
    "usage_source": "MEASURED",
}


class TestBuildEnvelopeBytesGoldenCase:
    def test_golden_record_shape_decodes_as_model_event_envelope(self) -> None:
        """The exact real on-wire field set must decode via the same model
        (and same required-field semantics) the gateway forwarder's
        ``_decode_message`` uses."""
        value = build_envelope_bytes(dict(_GOLDEN_ENRICHED_PAYLOAD))

        envelope = ModelEventEnvelope[dict[str, object]].model_validate_json(value)

        assert envelope.payload == _GOLDEN_ENRICHED_PAYLOAD
        assert envelope.event_type == TOPIC
        assert (
            str(envelope.correlation_id) == _GOLDEN_ENRICHED_PAYLOAD["correlation_id"]
        )

    def test_pre_fix_bare_dict_fails_the_same_decode(self) -> None:
        """Regression guard: publishing the bare enriched dict (the pre-fix
        behavior) must fail the forwarder's decode, proving the wrap is load
        bearing and not merely cosmetic."""
        bare_value = json.dumps(_GOLDEN_ENRICHED_PAYLOAD).encode()

        with pytest.raises(ValidationError, match="payload"):
            ModelEventEnvelope[dict[str, object]].model_validate_json(bare_value)

    def test_missing_correlation_id_does_not_raise(self) -> None:
        """A payload without a parseable correlation_id degrades gracefully
        (envelope.correlation_id=None) rather than raising -- publish must
        stay best-effort per the daemon's existing retry/quarantine model."""
        payload = dict(_GOLDEN_ENRICHED_PAYLOAD)
        payload["correlation_id"] = "not-a-uuid"

        value = build_envelope_bytes(payload)
        envelope = ModelEventEnvelope[dict[str, object]].model_validate_json(value)

        assert envelope.correlation_id is None
        assert envelope.payload["correlation_id"] == "not-a-uuid"


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
        envelope = json.loads(published_value)
        # OMN-16417: the wire record is now ModelEventEnvelope-shaped;
        # enrichment fields live under the envelope's payload, not top-level.
        published = envelope["payload"]
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
        envelope = json.loads(captured_value)
        assert captured_key == envelope["payload"]["idempotency_key"].encode()

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
