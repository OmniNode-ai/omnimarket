# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read every declared copy of the lab host table, name each differing field per host, append one row (OMN-22809).

The lab host table lives in several places: the packaged table, the config overlay of the internal
repository and the overlay home's copy. They have to say the same thing about each host, and a copy
that changed alone (a cap raised in one, an availability flipped in another) is the drift this read
names: the host, the field and the value in each copy. The copies and their paths are the overlay's;
nothing is spelled here. It writes reading rows only, never a gate, a stop or an admission.

A copy that cannot be read, or that holds no host list, gives an UNKNOWN row; copies that differ in
any field of any host give NOT_WORKING; copies that agree on every field of every host give WORKING.
NOT_WORKING is a reading, not a failure of the run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_table_copies_drift_compute.models import (
    ModelCopyValue,
    ModelFieldDifference,
    ModelHostTableDriftCommand,
    ModelHostTableDriftResult,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    ModelHostTableCopiesDriftOverlay,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.protocols import (
    CopyReadError,
    ProtocolCopySource,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=host-table-copies-drift"

#: One copy's hosts by name, each a mapping of field to value.
Table = Mapping[str, Mapping[str, Any]]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _value_token(text: str, limit: int) -> str:
    """A value as one cell-safe token, cut to ``limit`` characters."""
    token = "".join("_" if ch.isspace() or ch in "|=," else ch for ch in text)
    return token[:limit]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compare_copies(
    tables: Mapping[str, Table],
    *,
    absent_token: str,
    host_row_field: str,
) -> tuple[tuple[ModelFieldDifference, ...], int, int]:
    """Each differing field per host, in copy order: (differences, hosts compared, hosts differing).

    A host missing from some copies is one difference under ``host_row_field`` and its fields are
    compared among the copies that hold it. A field a copy lacks shows as ``absent_token``.
    """
    copies = list(tables)
    hosts = sorted({host for table in tables.values() for host in table})
    differences: list[ModelFieldDifference] = []
    differing: set[str] = set()
    for host in hosts:
        holders = [copy for copy in copies if host in tables[copy]]
        if len(holders) < len(copies):
            differences.append(
                ModelFieldDifference(
                    host=host,
                    field=host_row_field,
                    values=tuple(
                        ModelCopyValue(
                            copy_name=copy,
                            value="present" if copy in holders else absent_token,
                        )
                        for copy in copies
                    ),
                )
            )
            differing.add(host)
        fields = sorted({name for copy in holders for name in tables[copy][host]})
        for name in fields:
            values = tuple(
                ModelCopyValue(
                    copy_name=copy,
                    value=_json(tables[copy][host][name])
                    if name in tables[copy][host]
                    else absent_token,
                )
                for copy in holders
            )
            if len({item.value for item in values}) > 1:
                differences.append(
                    ModelFieldDifference(host=host, field=name, values=values)
                )
                differing.add(host)
    return tuple(differences), len(hosts), len(differing)


class HandlerHostTableCopiesDrift:
    """Run the drift read of one host's declared copies of the lab host table."""

    def __init__(
        self,
        *,
        overlay: ModelHostTableCopiesDriftOverlay,
        host: str,
        source: ProtocolCopySource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._source = source
        self._appender = appender
        self._clock = clock

    def handle(self, command: ModelHostTableDriftCommand) -> ModelHostTableDriftResult:
        if command.write and self._appender is None:
            return ModelHostTableDriftResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        return self._publish(self._read(command.correlation_id), now, command.write)

    def _unknown(self, correlation_id: str, reason: str) -> ModelHostTableDriftResult:
        return ModelHostTableDriftResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=(reason_token(reason),),
            copies=tuple(copy.name for copy in self._overlay.copies),
        )

    def _table(self, name: str) -> dict[str, Mapping[str, Any]]:
        """One copy as hosts by name; a copy that is not a host list raises ``CopyReadError``."""
        settings = self._overlay.table
        text = self._source.text(name)
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            raise CopyReadError(f"{name}: not-yaml") from None
        rows = document.get(settings.hosts_key) if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise CopyReadError(f"{name}: no-{settings.hosts_key}-list")
        hosts: dict[str, Mapping[str, Any]] = {}
        for number, row in enumerate(rows, start=1):
            host = row.get(settings.name_key) if isinstance(row, dict) else None
            if not isinstance(host, str) or not host:
                raise CopyReadError(f"{name}: row-{number}-has-no-{settings.name_key}")
            if host in hosts:
                raise CopyReadError(f"{name}: host-{host}-declared-twice")
            hosts[host] = {k: v for k, v in row.items() if k != settings.name_key}
        return hosts

    def _read(self, correlation_id: str) -> ModelHostTableDriftResult:
        tables: dict[str, dict[str, Mapping[str, Any]]] = {}
        for copy in self._overlay.copies:
            try:
                tables[copy.name] = self._table(copy.name)
            except CopyReadError as exc:
                return self._unknown(correlation_id, f"copy-unreadable-{exc}")
        differences, compared, differing = compare_copies(
            tables,
            absent_token=self._overlay.absent_token,
            host_row_field=self._overlay.host_row_field,
        )
        names = tuple(tables)
        if compared == 0:
            verdict = EnumReadingVerdict.UNKNOWN
            reasons: tuple[str, ...] = ("no-host-in-any-copy",)
        elif differences:
            verdict = EnumReadingVerdict.NOT_WORKING
            reasons = (f"drift-{len(differences)}-fields-{differing}-hosts",)
        else:
            verdict = EnumReadingVerdict.WORKING
            reasons = (f"copies-agree-{len(names)}-copies-{compared}-hosts",)
        return ModelHostTableDriftResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=verdict,
            outcome=EnumReadingOutcome.READ,
            reasons=reasons,
            copies=names,
            hosts_compared=compared,
            differing_hosts=differing,
            differences=differences,
        )

    def _named(self, result: ModelHostTableDriftResult) -> str:
        publish = self._overlay.publish
        named = [
            f"{item.host}.{_value_token(item.field, publish.value_chars)}="
            + "+".join(
                f"{value.copy_name}:{_value_token(value.value, publish.value_chars)}"
                for value in item.values
            )
            for item in result.differences[: publish.max_named]
        ]
        if len(result.differences) > publish.max_named:
            named.append(f"and-{len(result.differences) - publish.max_named}-more")
        return ",".join(named) or "none"

    def _publish(
        self, result: ModelHostTableDriftResult, now: datetime, write: bool
    ) -> ModelHostTableDriftResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(now), overlay_hash)))[:32]
        cells_by_name = {
            "copies": ",".join(result.copies),
            "hosts": str(result.hosts_compared),
            "differing_hosts": str(result.differing_hosts),
            "differing_fields": str(len(result.differences)),
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
    result: ModelHostTableDriftResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["host_table_copies_drift_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "copies": list(result.copies),
        "hosts_compared": result.hosts_compared,
        "differences": [item.model_dump(mode="json") for item in result.differences],
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["host_table_copies_drift_failed"]
            if result.failed
            else topics["host_table_copies_drift_written"],
            body,
        ),
        (
            topics["host_table_copies_drift_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerHostTableCopiesDrift, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelHostTableDriftCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (
                topics["host_table_copies_drift_run_failed"],
                json.dumps(failure).encode(),
            )
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerHostTableCopiesDrift",
    "answer_command",
    "compare_copies",
    "events_of",
]
