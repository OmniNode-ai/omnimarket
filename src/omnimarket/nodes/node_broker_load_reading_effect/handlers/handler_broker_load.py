# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read how loaded the dev broker is, judge it against overlay bars, append one reading row.

The seam measurement of piece 3 (bus, runtime and seams) of the base-first plan. The broker runs one
shard that holds every partition; when partitions per shard pass what a shard serves, its queue
grows, reactor stalls follow and a consumer cannot finish starting. This reading measures both
sides: the broker's counts and the time a consumer takes to start. WORKING needs partitions per
shard and the start time inside their bars. NOT_WORKING names which is over, the shards the bar
would need and the partitions that never carried a record. A broker or probe that cannot be read
gives UNKNOWN with a typed reason. The reading changes nothing on the broker and is never a gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    EnumConsumerStart,
    ModelBrokerLoadCommand,
    ModelBrokerLoadResult,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerLoadOverlay,
)
from omnimarket.nodes.node_broker_load_reading_effect.protocols import (
    BrokerMetricsError,
    ProtocolBrokerMetrics,
    ProtocolConsumerStart,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=broker-load"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerBrokerLoad:
    """Run the broker load reading once."""

    def __init__(
        self,
        *,
        overlay: ModelBrokerLoadOverlay,
        host: str,
        metrics: ProtocolBrokerMetrics,
        consumer: ProtocolConsumerStart,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._metrics = metrics
        self._consumer = consumer
        self._appender = appender
        self._clock = clock

    def handle(self, command: ModelBrokerLoadCommand) -> ModelBrokerLoadResult:
        if command.write and self._appender is None:
            return ModelBrokerLoadResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        return self._publish(self._read(command.correlation_id), now, command.write)

    def _unknown(self, correlation_id: str, reason: str) -> ModelBrokerLoadResult:
        return ModelBrokerLoadResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=(reason_token(reason),),
        )

    def _read(self, correlation_id: str) -> ModelBrokerLoadResult:
        bars = self._overlay.bars
        try:
            metrics = self._metrics.read()
        except BrokerMetricsError as exc:
            return self._unknown(correlation_id, f"metrics-unreadable-{exc}")
        consumer = self._consumer.measure(self._overlay.consumer.deadline_seconds)
        if consumer.outcome is EnumConsumerStart.UNREADABLE:
            return self._unknown(
                correlation_id,
                f"consumer-unreadable-{consumer.detail or 'transport'}",
            )
        per_shard = math.ceil(metrics.partitions / metrics.shards)
        needed = math.ceil(metrics.partitions / bars.max_partitions_per_shard)
        over = max(
            0, metrics.partitions - bars.max_partitions_per_shard * metrics.shards
        )
        reasons: list[str] = []
        if per_shard > bars.max_partitions_per_shard:
            reasons.append(
                f"partitions-per-shard-{per_shard}-over-{bars.max_partitions_per_shard}"
            )
        if consumer.outcome is EnumConsumerStart.TIMEOUT:
            reasons.append("consumer-start-timeout")
        elif (consumer.elapsed_ms or 0) > bars.max_consumer_start_ms:
            reasons.append(
                f"consumer-start-{consumer.elapsed_ms}ms-over-{bars.max_consumer_start_ms}ms"
            )
        return ModelBrokerLoadResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=(
                EnumReadingVerdict.NOT_WORKING
                if reasons
                else EnumReadingVerdict.WORKING
            ),
            outcome=EnumReadingOutcome.READ,
            reasons=tuple(reasons) or ("inside-both-bars",),
            metrics=metrics,
            consumer=consumer,
            partitions_per_shard=per_shard,
            shards_needed=needed,
            partitions_over_bar=over,
        )

    def _publish(
        self, result: ModelBrokerLoadResult, now: datetime, write: bool
    ) -> ModelBrokerLoadResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(now), overlay_hash)))[:32]
        metrics = result.metrics
        consumer = result.consumer
        bars = self._overlay.bars
        cells_by_name = {
            "topics": str(metrics.topics) if metrics else "unknown",
            "partitions": str(metrics.partitions) if metrics else "unknown",
            "shards": str(metrics.shards) if metrics else "unknown",
            "partitions_per_shard": str(result.partitions_per_shard)
            if result.partitions_per_shard is not None
            else "unknown",
            "empty_partitions": str(metrics.empty_partitions) if metrics else "unknown",
            "shards_needed": str(result.shards_needed)
            if result.shards_needed is not None
            else "unknown",
            "partitions_over_bar": str(result.partitions_over_bar)
            if result.partitions_over_bar is not None
            else "unknown",
            "consumer_start": consumer.outcome.value if consumer else "unknown",
            "consumer_start_ms": str(consumer.elapsed_ms)
            if consumer and consumer.elapsed_ms is not None
            else "none",
            "bar_partitions_per_shard": str(bars.max_partitions_per_shard),
            "bar_consumer_start_ms": str(bars.max_consumer_start_ms),
        }
        content = _sha(
            json.dumps(
                [
                    result.verdict.value,
                    result.outcome.value,
                    list(result.reasons),
                    cells_by_name,
                ],
                sort_keys=True,
            )
        )[:32]
        result = result.model_copy(update={"row_key": key})
        if not write:
            return result
        assert self._appender is not None
        publish = self._overlay.publish
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={','.join(result.reasons)}",
            *(f"{name}={value}" for name, value in cells_by_name.items()),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return result.model_copy(
                update={"ledger": f"append-failed:{appended.status}", "row": row}
            )
        return result.model_copy(
            update={
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )


def events_of(
    result: ModelBrokerLoadResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["broker_load_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "partitions_per_shard": result.partitions_per_shard,
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["broker_load_failed"]
            if result.failed
            else topics["broker_load_written"],
            body,
        ),
        (
            topics["broker_load_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerBrokerLoad, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelBrokerLoadCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["broker_load_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerBrokerLoad", "answer_command", "events_of", "load_topics"]
