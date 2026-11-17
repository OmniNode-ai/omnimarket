# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare each overlay value with the value its consumer depends on; name a park list held outside an overlay (OMN-23150).

Every setting lives in an overlay, and a hard-coded value fails. The gap this read closes is the one
between the two: a job-started hook refused a job under 5 GiB free while the overlay set the work
mounts to 3072 MiB. Nothing compared the overlay's value with the value the consumer read. Here each
consumer the overlay declares gives two values, the overlay's and the consumer's effective one (its
file, or its override variable when that is set); both are normalised into the overlay's base unit and
a difference reads NOT_WORKING with both values named. A park list defined in a file the overlay lists
as outside every overlay reads NOT_WORKING too. Nothing is spelled here: the consumers, paths, units
and patterns are the overlay's.

A file that cannot be read, or a key or pattern that finds nothing, gives UNKNOWN unless a definite
mismatch was also found, which stays NOT_WORKING. NOT_WORKING is a reading, not a failure of the run.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_compute.models import (
    ModelConsumerMismatch,
    ModelOverlayJoinCommand,
    ModelOverlayJoinResult,
    ModelParkListFinding,
)
from omnimarket.nodes.node_overlay_drift_compute.overlay import (
    ModelOverlayDriftComputeOverlay,
    ModelValueSource,
)
from omnimarket.nodes.node_overlay_drift_compute.protocols import (
    ProtocolFileSource,
    SourceReadError,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=overlay-consumer-join"
#: A quantity: a number with an optional unit name after it.
_QUANTITY = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)\s*$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _token(text: str, limit: int) -> str:
    """A value as one cell-safe token, cut to ``limit`` characters."""
    token = "".join("_" if ch.isspace() or ch in "|=,+" else ch for ch in text)
    return token[:limit]


def normalize(
    raw: str, unit: str | None, units: Mapping[str, int]
) -> tuple[str, str | None]:
    """(normalized value, problem). A quantity is in the base unit as a plain decimal; other text is compared as text.

    A number with a unit name after it uses that unit; a bare number uses ``unit`` (the source's
    declared one). A unit name the overlay does not declare is a problem, never a guess.
    """
    match = _QUANTITY.match(raw)
    if match is None:
        return f"text:{raw.strip()}", None
    suffix = match.group("unit")
    name = suffix or unit
    if name is None:
        return f"number:{Decimal(match.group('number')).normalize():f}", None
    if name not in units:
        return "", f"unit-{name}-not-declared"
    try:
        value = Decimal(match.group("number")) * units[name]
    except InvalidOperation:
        return "", "number-unreadable"
    return f"{value.normalize():f}", None


def _yaml_value(text: str, locator: str) -> str:
    try:
        node: object = yaml.safe_load(text)
    except yaml.YAMLError:
        raise SourceReadError("not-yaml") from None
    for part in locator.split("."):
        if not isinstance(node, dict) or part not in node:
            raise SourceReadError("key-not-found")
        node = node[part]
    if isinstance(node, (dict, list)) or node is None:
        raise SourceReadError("key-not-a-scalar")
    return str(node)


def read_value(source: ModelValueSource, files: ProtocolFileSource) -> str:
    """The raw value ``source`` declares; a file, key or pattern that gives none raises ``SourceReadError``."""
    text = files.text(source.path)
    if source.kind == "yaml_key":
        return _yaml_value(text, source.locator)
    found = re.search(source.locator, text, re.MULTILINE)
    if found is None:
        raise SourceReadError("pattern-not-found")
    value = found.group("value")
    if value is None or not value.strip():
        raise SourceReadError("pattern-found-no-value")
    return value.strip()


class HandlerOverlayDriftCompute:
    """Run the join check of one host's declared overlay values and their consumers."""

    def __init__(
        self,
        *,
        overlay: ModelOverlayDriftComputeOverlay,
        host: str,
        files: ProtocolFileSource,
        environ: Mapping[str, str],
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._files = files
        self._environ = environ
        self._appender = appender
        self._clock = clock

    def handle(self, request: ModelOverlayJoinCommand) -> ModelOverlayJoinResult:
        if request.write and self._appender is None:
            return ModelOverlayJoinResult(
                correlation_id=request.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        return self._publish(self._read(request.correlation_id), now, request.write)

    def _consumer_value(
        self, consumer_env: str | None, source: ModelValueSource
    ) -> tuple[str, str]:
        override = self._environ.get(consumer_env, "").strip() if consumer_env else ""
        if override:
            return override, "env"
        return read_value(source, self._files), "file"

    def _read(self, correlation_id: str) -> ModelOverlayJoinResult:
        overlay = self._overlay
        agreeing: list[str] = []
        mismatches: list[ModelConsumerMismatch] = []
        unreadable: list[str] = []
        for consumer in overlay.consumers:
            try:
                expected = read_value(consumer.overlay_value, self._files)
            except SourceReadError as exc:
                unreadable.append(f"{consumer.name}.overlay:{exc}")
                continue
            try:
                actual, via = self._consumer_value(
                    consumer.env_override, consumer.consumer_value
                )
            except SourceReadError as exc:
                unreadable.append(f"{consumer.name}.consumer:{exc}")
                continue
            expected_n, problem = normalize(
                expected, consumer.overlay_value.unit, overlay.units
            )
            if problem:
                unreadable.append(f"{consumer.name}.overlay:{problem}")
                continue
            actual_n, problem = normalize(
                actual, consumer.consumer_value.unit, overlay.units
            )
            if problem:
                unreadable.append(f"{consumer.name}.consumer:{problem}")
                continue
            if expected_n == actual_n:
                agreeing.append(consumer.name)
            else:
                mismatches.append(
                    ModelConsumerMismatch(
                        consumer=consumer.name,
                        overlay_value=expected,
                        consumer_value=actual,
                        overlay_normalized=expected_n,
                        consumer_normalized=actual_n,
                        consumer_via="env" if via == "env" else "file",
                    )
                )
        parks: list[ModelParkListFinding] = []
        for scan in overlay.park_list_scans:
            try:
                text = self._files.text(scan.path)
            except SourceReadError as exc:
                unreadable.append(f"{scan.name}:{exc}")
                continue
            pattern = re.compile(scan.pattern, re.MULTILINE)
            parks.extend(
                ModelParkListFinding(
                    scan=scan.name,
                    line=text.count("\n", 0, found.start()) + 1,
                    text=_clean(found.group(0)),
                )
                for found in pattern.finditer(text)
            )
        reasons: list[str] = []
        if mismatches:
            reasons.append(f"consumer-differs-from-overlay-{len(mismatches)}")
        if parks:
            reasons.append(f"park-list-outside-overlay-{len(parks)}")
        if unreadable:
            reasons.append(
                "source-unreadable-"
                + reason_token(",".join(item.replace(":", "-") for item in unreadable))
            )
        if mismatches or parks:
            verdict, outcome = EnumReadingVerdict.NOT_WORKING, EnumReadingOutcome.READ
        elif unreadable:
            verdict = EnumReadingVerdict.UNKNOWN
            outcome = EnumReadingOutcome.SOURCE_UNREADABLE
        else:
            verdict, outcome = EnumReadingVerdict.WORKING, EnumReadingOutcome.READ
            reasons.append(
                f"consumers-agree-{len(agreeing)}-no-park-list-{len(overlay.park_list_scans)}-scans"
            )
        return ModelOverlayJoinResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=verdict,
            outcome=outcome,
            reasons=tuple(reasons),
            consumers_checked=len(overlay.consumers),
            scans_checked=len(overlay.park_list_scans),
            agreeing=tuple(agreeing),
            mismatches=tuple(mismatches),
            park_lists=tuple(parks),
            unreadable=tuple(unreadable),
        )

    def _named(self, result: ModelOverlayJoinResult) -> str:
        publish = self._overlay.publish
        limit = publish.value_chars
        named = [
            f"{item.consumer}=overlay:{_token(item.overlay_value, limit)}"
            f"+consumer:{_token(item.consumer_value, limit)}"
            for item in result.mismatches
        ] + [f"park-list:{item.scan}:line{item.line}" for item in result.park_lists]
        shown = named[: publish.max_named]
        if len(named) > publish.max_named:
            shown.append(f"and-{len(named) - publish.max_named}-more")
        return ",".join(shown) or "none"

    def _publish(
        self, result: ModelOverlayJoinResult, now: datetime, write: bool
    ) -> ModelOverlayJoinResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(now), overlay_hash)))[:32]
        cells_by_name = {
            "base_unit": self._overlay.base_unit,
            "consumers": str(result.consumers_checked),
            "mismatched": str(len(result.mismatches)),
            "park_lists": str(len(result.park_lists)),
            "unreadable": str(len(result.unreadable)),
            "differing": self._named(result),
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
    result: ModelOverlayJoinResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["overlay_consumer_join_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "consumers_checked": result.consumers_checked,
        "agreeing": list(result.agreeing),
        "mismatches": [item.model_dump(mode="json") for item in result.mismatches],
        "park_lists": [item.model_dump(mode="json") for item in result.park_lists],
        "unreadable": list(result.unreadable),
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["overlay_consumer_join_failed"]
            if result.failed
            else topics["overlay_consumer_join_written"],
            body,
        ),
        (
            topics["overlay_consumer_join_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerOverlayDriftCompute, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelOverlayJoinCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (
                topics["overlay_consumer_join_run_failed"],
                json.dumps(failure).encode(),
            )
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerOverlayDriftCompute",
    "answer_command",
    "events_of",
    "normalize",
    "read_value",
]
