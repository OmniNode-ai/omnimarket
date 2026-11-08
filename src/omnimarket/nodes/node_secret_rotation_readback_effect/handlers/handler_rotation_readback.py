# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""After a rotation, read back every consumer: resolve its key, make it complete one call.

One row per consumer of the overlay's list goes to the ledger, each naming its consumer. A
consumer that cannot resolve its key, or whose call is refused because it is still on the old
key, reads NOT WORKING by name. A rotation without a named approver (listed in the overlay) is
refused before any probe, and nothing is written. Rows only, never a gate, a stop or an
admission (RULING 2026-11-06T12:05:56Z). No value, digest or length of a secret is in a row,
an event or a result.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.model_secret_rotation import (
    ModelRotationConsumerReadback,
    ModelRotationReadbackCommand,
    ModelRotationReadbackResult,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolSecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationConsumer,
    ModelRotationReadbackOverlay,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.protocols import (
    ProtocolConsumerCalls,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=secret-rotation-readback"
#: Values that record nothing: an approver cell holding one is no approver (OMN-18766).
_PLACEHOLDERS = frozenset({"", "unknown", "none", "tbd", "n/a", "-"})


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerRotationReadback:
    """``handle(ModelRotationReadbackCommand) -> ModelRotationReadbackResult``."""

    def __init__(
        self,
        *,
        overlay: ModelRotationReadbackOverlay,
        store: ProtocolSecretProbeStore,
        calls: ProtocolConsumerCalls,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        host: str,
    ) -> None:
        self._overlay = overlay
        self._store = store
        self._calls = calls
        self._appender = appender
        self._clock = clock
        self._host = host

    def handle(
        self, command: ModelRotationReadbackCommand
    ) -> ModelRotationReadbackResult:
        refusal = self._refusal(command)
        if refusal is not None:
            return ModelRotationReadbackResult(
                correlation_id=command.correlation_id,
                status="refused",
                failure=refusal,
            )
        now = self._clock().astimezone(UTC)
        readbacks = tuple(self._read_back(c) for c in self._overlay.consumers)
        reasons = tuple(
            f"consumer-{r.consumer}-{(r.call or r.resolve).value}"
            for r in readbacks
            if r.failed
        )
        unread = any(r.resolve.store_unread for r in readbacks)
        result = ModelRotationReadbackResult(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=(
                EnumReadingVerdict.NOT_WORKING
                if reasons
                else EnumReadingVerdict.WORKING
            ),
            outcome=(
                EnumReadingOutcome.SOURCE_UNREADABLE
                if unread
                else EnumReadingOutcome.READ
            ),
            reasons=reasons,
            consumers=readbacks,
        )
        if not command.write:
            return result
        return self._write(command, result, now)

    def _refusal(self, command: ModelRotationReadbackCommand) -> str | None:
        approver = command.approver.strip()
        if approver.lower() in _PLACEHOLDERS:
            return "rotation-without-approver"
        if approver not in self._overlay.rotation.approvers:
            return "approver-not-listed"
        if command.drill and not self._overlay.dev_only(
            command.rotated_folder, command.rotated_key
        ):
            return "drill-key-not-dev-only"
        if any(
            (c.folder, c.key) != (command.rotated_folder, command.rotated_key)
            for c in self._overlay.consumers
        ):
            return "consumer-not-of-rotated-key"
        if command.write and self._appender is None:
            return "no-appender-for-a-writing-run"
        return None

    def _read_back(
        self, consumer: ModelRotationConsumer
    ) -> ModelRotationConsumerReadback:
        # Only the outcome of the resolve is kept; the value stays unread inside the port's model.
        resolved = self._store.resolve(consumer.handle())
        if resolved.outcome.verdict is not EnumReadingVerdict.WORKING:
            return ModelRotationConsumerReadback(
                consumer=consumer.consumer,
                folder=consumer.folder,
                key=consumer.key,
                resolve=resolved.outcome,
                verdict=EnumReadingVerdict.NOT_WORKING,
            )
        call = self._calls.complete_call(consumer)
        return ModelRotationConsumerReadback(
            consumer=consumer.consumer,
            folder=consumer.folder,
            key=consumer.key,
            resolve=resolved.outcome,
            call=call,
            verdict=call.verdict,
        )

    def _write(
        self,
        command: ModelRotationReadbackCommand,
        result: ModelRotationReadbackResult,
        now: datetime,
    ) -> ModelRotationReadbackResult:
        assert self._appender is not None
        rows: list[str] = []
        statuses: list[str] = []
        for readback in result.consumers:
            row, request_id = self._row(command, readback, now)
            appended = self._appender.append(row + "\n", request_id)
            if not appended.ok:
                return result.model_copy(
                    update={
                        "status": "failed",
                        "ledger": f"append-failed:{appended.status}",
                        "rows": tuple(rows),
                        "failure": f"append-failed:{appended.status}",
                    }
                )
            rows.append(row)
            statuses.append(appended.status)
        ledger = (
            "written"
            if any(status == "accepted" for status in statuses)
            else "already-written"
        )
        return result.model_copy(update={"ledger": ledger, "rows": tuple(rows)})

    def _row(
        self,
        command: ModelRotationReadbackCommand,
        readback: ModelRotationConsumerReadback,
        now: datetime,
    ) -> tuple[str, str]:
        publish = self._overlay.publish
        outcome = (
            EnumReadingOutcome.SOURCE_UNREADABLE
            if readback.resolve.store_unread
            else EnumReadingOutcome.READ
        )
        key = _sha(
            "|".join(
                (
                    self._host,
                    "rotation",
                    command.rotation_id,
                    readback.consumer,
                    self._overlay.overlay_hash(),
                )
            )
        )[:32]
        content = _sha(
            json.dumps(
                [
                    readback.verdict.value,
                    readback.resolve.value,
                    readback.call.value if readback.call else None,
                ],
                sort_keys=True,
            )
        )[:32]
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"piece={publish.piece}",
            f"tag={'drill' if command.drill else 'operational'}",
            f"drill={'yes' if command.drill else 'no'}",
            f"rotation={_clean(command.rotation_id)}",
            f"approver={_clean(command.approver.strip())}",
            f"rotated={_clean(command.rotated_folder)}:{_clean(command.rotated_key)}",
            f"consumer={readback.consumer}",
            f"resolve={readback.resolve.value}",
            f"call={readback.call.value if readback.call else 'not-made'}",
            f"verdict={readback.verdict.value}",
            f"outcome={outcome.value}",
            f"backend={self._overlay.store.backend_name}",
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={self._overlay.overlay_hash()[:12]}",
        ]
        if command.drill:
            cells.append(_clean(f"injected fault: {self._overlay.injected_fault}"))
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        # The id names the whole row, so one id always carries the same bytes and a row that
        # differs in any cell (time, drill flag, approver, rotated handle) is another record.
        return row, str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}|{row}"))


def events_of(
    result: ModelRotationReadbackResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "refused":
        return [
            (
                topics["rotation_refused"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["rotation_consumer_failed"]
            if readback.failed
            else topics["rotation_consumer_passed"],
            {
                "correlation_id": result.correlation_id,
                "consumer": readback.consumer,
                "folder": readback.folder,
                "key": readback.key,
                "resolve": readback.resolve.value,
                "call": readback.call.value if readback.call else None,
                "verdict": readback.verdict.value,
            },
        )
        for readback in result.consumers
    ]
    if result.status == "failed":
        events.append(
            (
                topics["rotation_readback_failed"],
                {
                    "correlation_id": result.correlation_id,
                    "failure": result.failure,
                    "ledger": result.ledger,
                },
            )
        )
        return events
    assert result.verdict is not None
    events.append(
        (
            topics["rotation_readback_completed"],
            {
                "correlation_id": result.correlation_id,
                "verdict": result.verdict.value,
                "reasons": list(result.reasons),
                "consumers": len(result.consumers),
                "failed": sum(r.failed for r in result.consumers),
                "rows": len(result.rows),
                "ledger": result.ledger,
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerRotationReadback, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelRotationReadbackCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["rotation_readback_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerRotationReadback",
    "answer_command",
    "events_of",
    "load_topics",
]
