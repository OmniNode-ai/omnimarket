#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cost event publication daemon (OMN-10460).

Polls $OMNI_HOME/.onex_state/llm-cost-events/ for new .json files,
validates them, and publishes to Kafka topic TOPIC_LLM_CALL_COMPLETED (see omnimarket.events.topics).

Processed files move to published/; invalid files go to rejected/ with a .error sidecar.
Crash-safe: on restart, unpublished files in the spool root are reprocessed
(consumer dedup via idempotency_key handles duplicates).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aiokafka import AIOKafkaProducer

from omnimarket.events.topics import TOPIC_LLM_CALL_COMPLETED as TOPIC

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    "session_id",
    "model_id",
    "reporting_source",
    "usage_source",
    "correlation_id",
)

VALID_USAGE_SOURCES = frozenset({"MEASURED", "ESTIMATED", "UNKNOWN"})
VALID_REPORTING_SOURCES = frozenset(
    {"build-loop", "claude-session", "codex", "unknown"}
)

DEFAULT_BOOTSTRAP_SERVERS = "192.168.86.201:19092"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0


def compute_source_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_idempotency_key(
    reporting_source: str,
    session_id: str,
    correlation_id: str,
    model_id: str,
    source_file_sha256: str,
) -> str:
    raw = f"{reporting_source}:{session_id}:{correlation_id}:{model_id}:{source_file_sha256}"
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_event(payload: dict[str, object]) -> list[str]:
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"missing required field: {field}")

    usage_source = payload.get("usage_source")
    if usage_source is not None and usage_source not in VALID_USAGE_SOURCES:
        errors.append(
            f"invalid usage_source {usage_source!r}; must be one of {sorted(VALID_USAGE_SOURCES)}"
        )

    reporting_source = payload.get("reporting_source")
    if reporting_source is not None and reporting_source not in VALID_REPORTING_SOURCES:
        errors.append(
            f"invalid reporting_source {reporting_source!r}; must be one of {sorted(VALID_REPORTING_SOURCES)}"
        )

    correlation_id = payload.get("correlation_id")
    if correlation_id is not None:
        try:
            uuid.UUID(str(correlation_id))
        except ValueError:
            errors.append(f"invalid correlation_id {correlation_id!r}; must be a UUID")

    return errors


class CostEventPublisher:
    """Polls a spool directory and publishes cost events to Kafka."""

    def __init__(
        self,
        spool_dir: Path,
        bootstrap_servers: str = DEFAULT_BOOTSTRAP_SERVERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._spool_dir = spool_dir
        self._bootstrap_servers = bootstrap_servers
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds

    def _published_dir(self) -> Path:
        d = self._spool_dir / "published"
        d.mkdir(exist_ok=True)
        return d

    def _rejected_dir(self) -> Path:
        d = self._spool_dir / "rejected"
        d.mkdir(exist_ok=True)
        return d

    def _quarantine(self, file: Path, reason: str) -> None:
        rejected = self._rejected_dir()
        dest = rejected / file.name
        file.rename(dest)
        dest.with_suffix(dest.suffix + ".error").write_text(reason)
        logger.warning("Quarantined %s: %s", file.name, reason)

    async def process_file(self, file: Path) -> bool:
        """Validate and publish a single event file. Returns True on success."""
        # Parse JSON
        try:
            raw = file.read_bytes()
            payload: dict[str, object] = json.loads(raw)
        except Exception as exc:
            self._quarantine(file, f"JSON parse error: {exc}")
            return False

        # Validate schema
        errors = validate_event(payload)
        if errors:
            self._quarantine(file, "Validation errors:\n" + "\n".join(errors))
            return False

        # Compute derived fields
        file_sha256 = hashlib.sha256(raw).hexdigest()
        idempotency_key = compute_idempotency_key(
            reporting_source=str(payload["reporting_source"]),
            session_id=str(payload["session_id"]),
            correlation_id=str(payload["correlation_id"]),
            model_id=str(payload["model_id"]),
            source_file_sha256=file_sha256,
        )

        enriched: dict[str, object] = {
            **payload,
            "idempotency_key": idempotency_key,
            "source_file_sha256": file_sha256,
            "emitted_at": datetime.now(UTC).isoformat(),
        }

        value = json.dumps(enriched).encode()
        key = idempotency_key.encode()

        # Publish with retry
        producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                await producer.send_and_wait(TOPIC, value=value, key=key)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Kafka publish attempt %d/%d failed for %s: %s",
                    attempt,
                    self._max_retries,
                    file.name,
                    exc,
                )
                if attempt < self._max_retries and self._retry_backoff_seconds > 0:
                    await asyncio.sleep(self._retry_backoff_seconds)

        if last_exc is not None:
            self._quarantine(
                file,
                f"Kafka publish failed after {self._max_retries} attempts: {last_exc}",
            )
            return False

        # Move to published/
        dest = self._published_dir() / file.name
        file.rename(dest)
        logger.info(
            "Published and archived %s (idempotency_key=%s)", file.name, idempotency_key
        )
        return True

    async def poll_once(self) -> int:
        """Process all pending .json files in the spool root. Returns count processed."""
        files = [
            f for f in self._spool_dir.iterdir() if f.is_file() and f.suffix == ".json"
        ]
        if not files:
            return 0

        count = 0
        for f in sorted(files):
            await self.process_file(f)
            count += 1
        return count

    async def run(self, poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        """Run the daemon loop indefinitely."""
        logger.info(
            "Cost event publisher daemon starting (spool=%s, topic=%s, broker=%s)",
            self._spool_dir,
            TOPIC,
            self._bootstrap_servers,
        )
        while True:
            try:
                n = await self.poll_once()
                if n:
                    logger.info("Processed %d event file(s)", n)
            except Exception:
                logger.exception("Unexpected error in poll_once")
            await asyncio.sleep(poll_interval)


def _spool_dir_from_env() -> Path:
    omni_home = Path(os.environ["OMNI_HOME"])
    return omni_home / ".onex_state" / "llm-cost-events"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    spool_dir = _spool_dir_from_env()
    spool_dir.mkdir(parents=True, exist_ok=True)

    bootstrap_servers = os.environ.get(
        "KAFKA_BOOTSTRAP_SERVERS", DEFAULT_BOOTSTRAP_SERVERS
    )
    poll_interval = float(
        os.environ.get(
            "COST_PUBLISHER_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_SECONDS)
        )
    )

    publisher = CostEventPublisher(
        spool_dir=spool_dir,
        bootstrap_servers=bootstrap_servers,
    )
    asyncio.run(publisher.run(poll_interval=poll_interval))


if __name__ == "__main__":
    main()
