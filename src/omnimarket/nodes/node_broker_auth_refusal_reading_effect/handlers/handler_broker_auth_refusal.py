# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the dev broker's authentication refusals, judge them against an overlay bar, append one reading row.

A client that dials a SASL listener and sends a request before it authenticates is refused by the broker
(``kafka_authentication_exception``) and, in a client that retries, reconnects and is refused again. The
broker's log was the only place that showed it (OMN-19044). This reading counts those lines over a window.
WORKING needs the count at or under the overlay's bar. NOT_WORKING names the count, the first and last
time, the minutes that held any and the first line. A log that cannot be read gives UNKNOWN with a typed
reason. The reading changes nothing on the broker and is never a gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.models.model_broker_auth_refusal import (
    ModelBrokerAuthRefusalCommand,
    ModelBrokerAuthRefusalResult,
    ModelRefusalForm,
    ModelRefusalMinute,
    ModelRefusalWindow,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    ModelBrokerAuthRefusalOverlay,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.protocols import (
    BrokerLogError,
    ControlRun,
    ProtocolBrokerLog,
    ProtocolControlRuns,
    parse_stamp,
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
_ROW_SOURCE_CELL = "src=broker-auth-refusal"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _sort_key(line: str) -> tuple[int, datetime]:
    stamp = parse_stamp(line.split(" ", 1)[0])
    return (0, stamp) if stamp else (1, datetime.min.replace(tzinfo=UTC))


def split_control(
    lines: Sequence[str],
    controls: Sequence[ControlRun],
    tolerance_seconds: float,
    max_lines_per_run: int,
) -> tuple[list[str], list[str]]:
    """Split refusal lines into (control, other) by the control's own marker.

    A line is the control when its time falls inside a control run's span (its container's start to finish, plus
    the slack) and that run has not yet explained ``max_lines_per_run`` lines. A line past that count, a line
    outside every span and a line with no readable time are another client.
    """
    slack = timedelta(seconds=tolerance_seconds)
    taken = [0] * len(controls)
    control: list[str] = []
    other: list[str] = []
    for line in sorted(lines, key=_sort_key):
        stamp = parse_stamp(line.split(" ", 1)[0])
        owner = None
        if stamp is not None:
            for index, run in enumerate(controls):
                if (
                    run.started_at - slack <= stamp <= run.finished_at + slack
                    and taken[index] < max_lines_per_run
                ):
                    owner = index
                    break
        if owner is None:
            other.append(line)
        else:
            taken[owner] += 1
            control.append(line)
    return control, other


def _seconds(lines: Sequence[str]) -> list[str]:
    found: list[str] = []
    for line in lines:
        stamp = line.split(" ", 1)[0]
        if len(stamp) >= 19 and stamp[4] == "-" and stamp[10] == "T":
            found.append(stamp[:19])
    return found


def summarize(
    lines: Sequence[str],
    *,
    sample_max_chars: int,
    forms: Mapping[str, str],
    controls: Sequence[ControlRun] = (),
    tolerance_seconds: float = 0,
    max_lines_per_run: int = 1,
) -> ModelRefusalWindow:
    """Count refusal lines, the control apart from every other. A line starts with its RFC 3339 timestamp.

    A line whose timestamp cannot be read still counts, as another client: a refusal with no time is a refusal.
    """
    control_lines, other_lines = split_control(
        lines, controls, tolerance_seconds, max_lines_per_run
    )
    other_seconds = _seconds(other_lines)
    control_seconds = _seconds(control_lines)
    per_minute = Counter(stamp[:16] for stamp in other_seconds)
    per_form: Counter[str] = Counter()
    for line in other_lines:
        name = next((n for n, text in forms.items() if text in line), "unnamed-form")
        per_form[name] += 1
    first_line = other_lines[0] if other_lines else ""
    sample = first_line.split(" ", 1)[1] if " " in first_line else first_line
    return ModelRefusalWindow(
        total=len(lines),
        control=len(control_lines),
        other=len(other_lines),
        control_runs=len(controls),
        minutes=tuple(
            ModelRefusalMinute(minute=minute, count=count)
            for minute, count in sorted(per_minute.items())
        ),
        first_seen=min(other_seconds) if other_seconds else "",
        last_seen=max(other_seconds) if other_seconds else "",
        control_first_seen=min(control_seconds) if control_seconds else "",
        control_last_seen=max(control_seconds) if control_seconds else "",
        forms=tuple(
            ModelRefusalForm(form=name, count=count)
            for name, count in sorted(per_form.items())
        ),
        sample=_clean(sample)[:sample_max_chars],
    )


class HandlerBrokerAuthRefusal:
    """Run the broker authentication refusal reading once."""

    def __init__(
        self,
        *,
        overlay: ModelBrokerAuthRefusalOverlay,
        host: str,
        source: ProtocolBrokerLog,
        control_runs: ProtocolControlRuns,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._source = source
        self._control_runs = control_runs
        self._appender = appender
        self._clock = clock

    def handle(
        self, command: ModelBrokerAuthRefusalCommand
    ) -> ModelBrokerAuthRefusalResult:
        if command.write and self._appender is None:
            return ModelBrokerAuthRefusalResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        return self._publish(self._read(command.correlation_id), now, command.write)

    def _read(self, correlation_id: str) -> ModelBrokerAuthRefusalResult:
        reading = self._overlay.reading
        try:
            lines = self._source.matching_lines(reading.needle, reading.window_seconds)
        except BrokerLogError as exc:
            return ModelBrokerAuthRefusalResult(
                correlation_id=correlation_id,
                status="completed",
                host=self._host,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=(reason_token(f"log-unreadable-{exc}"),),
            )
        try:
            controls = self._control_runs.runs()
        except BrokerLogError as exc:
            return ModelBrokerAuthRefusalResult(
                correlation_id=correlation_id,
                status="completed",
                host=self._host,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=(reason_token(f"control-unreadable-{exc}"),),
            )
        marker = self._overlay.control
        window = summarize(
            lines,
            sample_max_chars=reading.sample_max_chars,
            forms=reading.forms,
            controls=controls,
            tolerance_seconds=marker.tolerance_seconds,
            max_lines_per_run=marker.max_lines_per_run,
        )
        bar = self._overlay.bars.max_refusals
        over = window.other > bar
        return ModelBrokerAuthRefusalResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=EnumReadingVerdict.NOT_WORKING
            if over
            else EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=(f"other-refusals-{window.other}-over-{bar}",)
            if over
            else ("inside-the-bar",),
            window=window,
        )

    def _publish(
        self, result: ModelBrokerAuthRefusalResult, now: datetime, write: bool
    ) -> ModelBrokerAuthRefusalResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(now), overlay_hash)))[:32]
        window = result.window
        cells_by_name = {
            "refusals": str(window.total) if window else "unknown",
            "control_refusals": str(window.control) if window else "unknown",
            "other_refusals": str(window.other) if window else "unknown",
            "control_runs_held": str(window.control_runs) if window else "unknown",
            "minutes_with_refusals": str(len(window.minutes)) if window else "unknown",
            "first_seen": (window.first_seen or "none") if window else "unknown",
            "last_seen": (window.last_seen or "none") if window else "unknown",
            "control_last_seen": (window.control_last_seen or "none")
            if window
            else "unknown",
            "other_forms": (
                ",".join(f"{f.form}:{f.count}" for f in window.forms) or "none"
            )
            if window
            else "unknown",
            "window_seconds": str(self._overlay.reading.window_seconds),
            "bar_max_refusals": str(self._overlay.bars.max_refusals),
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
            *([f"sample={window.sample}"] if window and window.sample else []),
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
    result: ModelBrokerAuthRefusalResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["broker_auth_refusal_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "refusals": result.window.total if result.window else None,
        "control_refusals": result.window.control if result.window else None,
        "other_refusals": result.window.other if result.window else None,
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["broker_auth_refusal_failed"]
            if result.failed
            else topics["broker_auth_refusal_written"],
            body,
        ),
        (
            topics["broker_auth_refusal_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerBrokerAuthRefusal, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelBrokerAuthRefusalCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (topics["broker_auth_refusal_run_failed"], json.dumps(failure).encode())
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerBrokerAuthRefusal",
    "answer_command",
    "events_of",
    "load_topics",
    "split_control",
    "summarize",
]
