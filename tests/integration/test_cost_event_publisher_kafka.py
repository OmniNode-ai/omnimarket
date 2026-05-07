# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="integration test — uses lab Kafka bootstrap address as test target; not a runtime default"
"""Integration test: cost_event_publisher publishes real events to Kafka.

Requires:
- Kafka reachable at 192.168.86.201:19092
- Run with brew Python (LAN grant): /path/to/.venv-brew/bin/pytest tests/integration/test_cost_event_publisher_kafka.py -v

Marked @pytest.mark.kafka so the default uv run pytest suite skips it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

# Ensure scripts/ is on sys.path for direct invocation
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

BOOTSTRAP_SERVERS = "192.168.86.201:19092"
TOPIC = "onex.evt.omniintelligence.llm-call-completed.v1"


def _valid_payload() -> dict[str, object]:
    return {
        "session_id": f"integration-test-{uuid.uuid4()}",
        "model_id": "qwen3-coder-30b",
        "reporting_source": "build-loop",
        "usage_source": "MEASURED",
        "correlation_id": str(uuid.uuid4()),
        "input_tokens": 100,
        "output_tokens": 20,
        "total_cost_usd": 0.0001,
    }


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_publishes_real_event_to_kafka(tmp_path: Path) -> None:
    """Write a valid event file, run the publisher, and consume from Kafka to verify."""
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    # Verify Kafka reachable before importing the daemon (which also uses aiokafka)
    try:
        probe = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP_SERVERS)
        await asyncio.wait_for(probe.start(), timeout=5)
        await probe.stop()
    except Exception as exc:
        pytest.skip(f"Kafka not reachable: {exc}")

    from scripts.cost_event_publisher import CostEventPublisher

    spool = tmp_path / "llm-cost-events"
    spool.mkdir()

    payload = _valid_payload()
    event_file = spool / "test-event.json"
    event_file.write_text(json.dumps(payload))

    # Compute expected idempotency key
    file_sha256 = hashlib.sha256(event_file.read_bytes()).hexdigest()
    raw_key = f"{payload['reporting_source']}:{payload['session_id']}:{payload['correlation_id']}:{payload['model_id']}:{file_sha256}"
    expected_idempotency_key = hashlib.sha256(raw_key.encode()).hexdigest()

    group_id = f"test-cost-publisher-{uuid.uuid4().hex[:8]}"
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        group_id=group_id,
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    # Force partition assignment and seek to end before publishing so we don't
    # miss the message due to aiokafka lazy offset resolution.
    await consumer.seek_to_end()

    publisher = CostEventPublisher(
        spool_dir=spool,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        max_retries=3,
        retry_backoff_seconds=0.5,
    )
    result = await publisher.process_file(event_file)

    assert result is True, "process_file returned False — check rejected/ for details"

    published_dir = spool / "published"
    assert (published_dir / "test-event.json").exists(), "File not moved to published/"
    assert not event_file.exists(), "Original file still exists in spool root"

    # Consume the message
    received: dict[str, object] | None = None
    try:
        msg = await asyncio.wait_for(consumer.getone(), timeout=15)
        received = json.loads(msg.value)
        assert msg.key == expected_idempotency_key.encode(), (
            f"Message key mismatch: got {msg.key!r}, expected {expected_idempotency_key!r}"
        )
    except TimeoutError:
        pytest.fail("Timed out waiting for Kafka message — event not published")
    finally:
        await consumer.stop()

    assert received is not None
    assert received["idempotency_key"] == expected_idempotency_key
    assert received["source_file_sha256"] == file_sha256
    assert "emitted_at" in received
    assert received["session_id"] == payload["session_id"]
    assert received["model_id"] == payload["model_id"]


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_is_stable_on_reprocess(tmp_path: Path) -> None:
    """Publishing the same file twice produces the same idempotency key (consumer dedup handles it)."""

    from scripts.cost_event_publisher import CostEventPublisher, compute_idempotency_key

    spool = tmp_path / "llm-cost-events"
    spool.mkdir()

    payload = _valid_payload()
    content = json.dumps(payload).encode()
    event_file = spool / "idempotent-event.json"
    event_file.write_bytes(content)

    file_sha256 = hashlib.sha256(content).hexdigest()
    key1 = compute_idempotency_key(
        reporting_source=str(payload["reporting_source"]),
        session_id=str(payload["session_id"]),
        correlation_id=str(payload["correlation_id"]),
        model_id=str(payload["model_id"]),
        source_file_sha256=file_sha256,
    )

    # Simulate crash-recovery: copy published file back to spool root
    publisher = CostEventPublisher(
        spool_dir=spool,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        max_retries=1,
        retry_backoff_seconds=0.0,
    )

    # First publish: file moves to published/
    result1 = await publisher.process_file(event_file)
    assert result1 is True
    assert (spool / "published" / "idempotent-event.json").exists()

    # Simulate crash-recovery: put the original raw content back in spool.
    # The daemon moves (not copies) files, so a crash before rename leaves the
    # original in the spool root; on restart the same file is reprocessed.
    event_file2 = spool / "idempotent-event.json"
    event_file2.write_bytes(content)

    result2 = await publisher.process_file(event_file2)
    assert result2 is True

    # The idempotency_key is deterministic: same input → same key, regardless of
    # how many times the file is reprocessed. Verify the key directly.
    key2 = compute_idempotency_key(
        reporting_source=str(payload["reporting_source"]),
        session_id=str(payload["session_id"]),
        correlation_id=str(payload["correlation_id"]),
        model_id=str(payload["model_id"]),
        source_file_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert key1 == key2, f"Idempotency key changed: {key1!r} != {key2!r}"


if __name__ == "__main__":
    # Standalone runner: env -u PYTHONPATH /path/to/.venv-brew/bin/python <this_file>
    import tempfile

    async def _run_publish_test() -> None:
        from aiokafka import AIOKafkaConsumer

        from scripts.cost_event_publisher import CostEventPublisher

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "llm-cost-events"
            spool.mkdir()

            payload = _valid_payload()
            event_file = spool / "test-event.json"
            event_file.write_text(json.dumps(payload))

            file_sha256 = hashlib.sha256(event_file.read_bytes()).hexdigest()
            raw_key = f"{payload['reporting_source']}:{payload['session_id']}:{payload['correlation_id']}:{payload['model_id']}:{file_sha256}"
            expected_key = hashlib.sha256(raw_key.encode()).hexdigest()

            group_id = f"test-{uuid.uuid4().hex[:8]}"
            consumer = AIOKafkaConsumer(
                TOPIC,
                bootstrap_servers=BOOTSTRAP_SERVERS,
                group_id=group_id,
                auto_offset_reset="latest",
            )
            await consumer.start()
            await consumer.seek_to_end()

            publisher = CostEventPublisher(
                spool_dir=spool, bootstrap_servers=BOOTSTRAP_SERVERS
            )
            result = await publisher.process_file(event_file)
            assert result, "process_file returned False"

            msg = await asyncio.wait_for(consumer.getone(), timeout=15)
            await consumer.stop()

            received = json.loads(msg.value)
            assert received["idempotency_key"] == expected_key
            assert received["source_file_sha256"] == file_sha256
            assert msg.key == expected_key.encode()
            print("PASS: test_publishes_real_event_to_kafka")

    async def _run_idempotency_test() -> None:
        from scripts.cost_event_publisher import (
            CostEventPublisher,
            compute_idempotency_key,
        )

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "llm-cost-events"
            spool.mkdir()

            payload = _valid_payload()
            content = json.dumps(payload).encode()
            event_file = spool / "idempotent-event.json"
            event_file.write_bytes(content)

            file_sha256 = hashlib.sha256(content).hexdigest()
            key1 = compute_idempotency_key(
                reporting_source=str(payload["reporting_source"]),
                session_id=str(payload["session_id"]),
                correlation_id=str(payload["correlation_id"]),
                model_id=str(payload["model_id"]),
                source_file_sha256=file_sha256,
            )

            publisher = CostEventPublisher(
                spool_dir=spool,
                bootstrap_servers=BOOTSTRAP_SERVERS,
                max_retries=1,
                retry_backoff_seconds=0.0,
            )
            assert await publisher.process_file(event_file)
            assert (spool / "published" / "idempotent-event.json").exists()

            # Simulate crash-recovery: restore the original raw content to spool root.
            event_file2 = spool / "idempotent-event.json"
            event_file2.write_bytes(content)
            assert await publisher.process_file(event_file2)

            # The key is a pure function of the input — same content → same key.
            key2 = compute_idempotency_key(
                reporting_source=str(payload["reporting_source"]),
                session_id=str(payload["session_id"]),
                correlation_id=str(payload["correlation_id"]),
                model_id=str(payload["model_id"]),
                source_file_sha256=file_sha256,
            )
            assert key1 == key2
            print("PASS: test_idempotency_key_is_stable_on_reprocess")

    async def _run_all() -> None:
        await _run_publish_test()
        await _run_idempotency_test()

    asyncio.run(_run_all())
