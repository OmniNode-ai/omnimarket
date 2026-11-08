# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read every pinned overlay in the overlay home through the runtime's own models; append one row.

The reading of piece 4 (contracts, overlays and injected dependencies) of the base-first plan.
The overlay home's ci validates each overlay at the omnimarket revision its pin names; this read
validates the same overlays through the models the running runtime has installed, which is where
an overlay key the runtime rejects shows (the rung_order case). It writes reading rows only,
never a gate, a stop or an admission. A home or runtime that cannot be read gives an UNKNOWN row;
a refused, absent or unloadable overlay gives NOT_WORKING. An overlay whose model the runtime
lacks is the runtime lagging the models, not drift: it is counted as model_missing and does not by
itself fail the reading. A run where every overlay the runtime could judge was accepted gives
WORKING; a run where the runtime could judge none gives UNKNOWN. NOT_WORKING is a reading, not a
failure of the run.
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

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    EnumOverlayVerdict,
    ModelOverlayCheck,
    ModelOverlayDriftCommand,
    ModelOverlayDriftCounts,
    ModelOverlayDriftResult,
    ModelOverlayPin,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayDriftOverlay,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    OverlayHomeError,
    PinHistoryError,
    ProtocolOverlayHome,
    ProtocolPinHistory,
    ProtocolRuntimeValidator,
    RuntimeValidatorError,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
UPSTREAM_PREFIX = "upstream_"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=overlay-drift"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _cell_token(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-<>" else "-" for ch in text)


class HandlerOverlayDrift:
    """Run the drift read of one overlay home against one runtime."""

    def __init__(
        self,
        *,
        overlay: ModelOverlayDriftOverlay,
        host: str,
        home: ProtocolOverlayHome,
        validator: ProtocolRuntimeValidator,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        pin_history: ProtocolPinHistory | None = None,
        emissions: ProtocolEmissionSink | None = None,
    ) -> None:
        self._emissions = emissions
        self._pin_history = pin_history
        self._overlay = overlay
        self._host = host
        self._home = home
        self._validator = validator
        self._appender = appender
        self._clock = clock

    def handle(self, command: ModelOverlayDriftCommand) -> ModelOverlayDriftResult:
        chain = self._chain(command.correlation_id)
        if command.upstream_failure is not None:
            return self._refuse(command, chain is not None)
        if chain is not None:
            chain.start()
        result = self._run(command)
        if chain is not None:
            self._close_chain(chain, result)
        return result

    def _chain(self, correlation_id: str) -> ChainEmitter | None:
        emission = self._overlay.emission
        if emission is None or self._emissions is None:
            return None
        return ChainEmitter(
            self._emissions,
            node=emission.node,
            piece=emission.piece,
            clock=self._clock,
            correlation_id=correlation_id,
        )

    def _refuse(
        self, command: ModelOverlayDriftCommand, emit: bool
    ) -> ModelOverlayDriftResult:
        """The piece upstream failed: no start, no reading, one typed failure under the same correlation id."""
        code = command.upstream_failure or ""
        if not code.startswith(UPSTREAM_PREFIX):
            code = f"{UPSTREAM_PREFIX}{code}"
        emission = self._overlay.emission
        if emit and emission is not None and self._emissions is not None:
            self._emissions.emit(
                ModelNodeEmission(
                    node=emission.node,
                    piece=emission.piece,
                    correlation_id=command.correlation_id,
                    phase="failure",
                    emitted_at=self._clock(),
                    failure_code=code,
                    failure_detail="the upstream piece failed; no overlay was read",
                )
            )
        return ModelOverlayDriftResult(
            correlation_id=command.correlation_id,
            status="failed",
            host=self._host,
            failure=code,
        )

    def _close_chain(
        self, chain: ChainEmitter, result: ModelOverlayDriftResult
    ) -> None:
        """A WORKING reading closes the chain with a success; every other end is a typed failure naming the key."""
        if result.status == "failed":
            chain.failure("run_failed", result.failure or "")
        elif result.failed:
            chain.failure(
                "reading_not_written",
                ",".join(result.reasons) or result.ledger,
            )
        elif result.verdict is EnumReadingVerdict.WORKING:
            chain.success(
                {
                    "checked": result.counts.checked,
                    "accepted": result.counts.accepted,
                }
            )
        elif result.verdict is EnumReadingVerdict.NOT_WORKING:
            chain.failure("overlay_not_accepted", self._named(result))
        else:
            chain.failure("reading_unknown", ",".join(result.reasons))

    def _run(self, command: ModelOverlayDriftCommand) -> ModelOverlayDriftResult:
        if command.write and self._appender is None:
            return ModelOverlayDriftResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        result = self._read(command.correlation_id)
        return self._publish(result, now, command.write, command.upstream_cid)

    def _unknown(self, correlation_id: str, reason: str) -> ModelOverlayDriftResult:
        return ModelOverlayDriftResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=(reason_token(reason),),
            home_revision=self._home.revision(),
        )

    def _read(self, correlation_id: str) -> ModelOverlayDriftResult:
        try:
            pins = self._home.pins()
            held = set(self._home.overlay_ids())
            pinned = {pin.overlay_id for pin in pins}
            entries: list[tuple[ModelOverlayPin, str]] = [
                (pin, self._home.text(pin.overlay_id))
                for pin in pins
                if pin.overlay_id in held
            ]
        except OverlayHomeError as exc:
            return self._unknown(correlation_id, f"home-unreadable-{exc}")
        try:
            answer = self._validator.validate(tuple(entries))
        except RuntimeValidatorError as exc:
            return self._unknown(correlation_id, f"runtime-unreadable-{exc}")
        asked = {pin.overlay_id for pin, _ in entries}
        answered = {check.overlay_id for check in answer.results}
        if answered != asked or len(answer.results) != len(asked):
            return self._unknown(
                correlation_id, "runtime-answer-does-not-match-request"
            )
        missing = tuple(
            ModelOverlayCheck(
                overlay_id=pin.overlay_id, verdict=EnumOverlayVerdict.OVERLAY_MISSING
            )
            for pin in pins
            if pin.overlay_id not in held
        )
        checks = (*answer.results, *missing)

        def count(verdict: EnumOverlayVerdict) -> int:
            return sum(check.verdict is verdict for check in checks)

        unpinned = tuple(sorted(held - pinned))
        unmerged_pins, pin_reason = self._unmerged_pins(pins)
        counts = ModelOverlayDriftCounts(
            checked=len(answer.results),
            accepted=count(EnumOverlayVerdict.ACCEPTED),
            rejected=count(EnumOverlayVerdict.REJECTED),
            model_missing=count(EnumOverlayVerdict.MODEL_MISSING),
            unreadable=count(EnumOverlayVerdict.UNREADABLE),
            overlay_missing=count(EnumOverlayVerdict.OVERLAY_MISSING),
            unpinned=len(unpinned),
            pin_unmerged=len(unmerged_pins),
        )
        failing = tuple(
            sorted(
                (c for c in checks if c.verdict is not EnumOverlayVerdict.ACCEPTED),
                key=lambda c: c.overlay_id,
            )
        )
        if counts.drift:
            verdict = EnumReadingVerdict.NOT_WORKING
            reasons = tuple(
                f"{verdict_name}-{getattr(counts, verdict_name)}"
                for verdict_name in (
                    "rejected",
                    "model_missing",
                    "unreadable",
                    "overlay_missing",
                )
                if getattr(counts, verdict_name)
            )
        elif counts.checked == 0:
            verdict = EnumReadingVerdict.UNKNOWN
            reasons = ("no-pinned-overlay-checked",)
        elif counts.accepted == 0:
            # Every overlay the runtime could read had a model it lacks: nothing was judged.
            verdict = EnumReadingVerdict.UNKNOWN
            reasons = (
                f"model_missing-{counts.model_missing}",
                "no-overlay-judged",
            )
        elif counts.model_missing:
            verdict = EnumReadingVerdict.WORKING
            reasons = (
                f"all-{counts.accepted}-judged-overlays-accepted",
                f"model_missing-{counts.model_missing}",
            )
        else:
            verdict = EnumReadingVerdict.WORKING
            reasons = (f"all-{counts.checked}-pinned-overlays-accepted",)
        # An unmerged pin is reported on its own count; it does not decide the verdict.
        if counts.pin_unmerged:
            reasons = (*reasons, f"pin_unmerged-{counts.pin_unmerged}")
        if pin_reason:
            reasons = (*reasons, pin_reason)
        return ModelOverlayDriftResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=verdict,
            outcome=EnumReadingOutcome.READ,
            reasons=reasons,
            counts=counts,
            failing=failing,
            unpinned=unpinned,
            unmerged_pins=unmerged_pins,
            runtime=answer.runtime,
            home_revision=self._home.revision(),
        )

    def _unmerged_pins(
        self, pins: tuple[ModelOverlayPin, ...]
    ) -> tuple[tuple[str, ...], str]:
        """Overlays whose pinned revision the merged branch does not hold, and a reason when unread."""
        if self._pin_history is None:
            return (), ""
        try:
            unmerged = self._pin_history.unmerged(tuple(pin.revision for pin in pins))
        except PinHistoryError as exc:
            return (), reason_token(f"pin-history-unreadable-{exc}")
        return tuple(
            sorted(pin.overlay_id for pin in pins if pin.revision in unmerged)
        ), ""

    def _named_pins(self, result: ModelOverlayDriftResult) -> str:
        limit = self._overlay.publish.max_named
        named = list(result.unmerged_pins[:limit])
        if len(result.unmerged_pins) > limit:
            named.append(f"and-{len(result.unmerged_pins) - limit}-more")
        return ",".join(named) or "none"

    def _named(self, result: ModelOverlayDriftResult) -> str:
        limit = self._overlay.publish.max_named
        named = [
            f"{check.overlay_id}:{check.verdict.value}"
            + (
                ":" + "+".join(_cell_token(f) for f in check.fields)
                if check.fields
                else ""
            )
            for check in result.failing[:limit]
        ]
        if len(result.failing) > limit:
            named.append(f"and-{len(result.failing) - limit}-more")
        return ",".join(named) or "none"

    def _publish(
        self,
        result: ModelOverlayDriftResult,
        now: datetime,
        write: bool,
        upstream_cid: str | None = None,
    ) -> ModelOverlayDriftResult:
        overlay_hash = self._overlay.overlay_hash()
        runtime = self._overlay.runtime
        key = _sha("|".join((self._host, runtime.name, stamp_text(now), overlay_hash)))[
            :32
        ]
        counts = result.counts
        cells_by_name = {
            "runtime": runtime.name,
            "runtime_version": result.runtime.version if result.runtime else "unknown",
            "runtime_revision": (
                result.runtime.revision[:12] if result.runtime else "unknown"
            ),
            "home_revision": result.home_revision[:12],
            "checked": str(counts.checked),
            "accepted": str(counts.accepted),
            "rejected": str(counts.rejected),
            "model_missing": str(counts.model_missing),
            "unreadable": str(counts.unreadable),
            "overlay_missing": str(counts.overlay_missing),
            "unpinned": str(counts.unpinned),
            "pin_unmerged": str(counts.pin_unmerged),
            "failing": self._named(result),
            "unmerged_pins": self._named_pins(result),
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
            *(
                (f"piece={self._overlay.emission.piece}",)
                if self._overlay.emission
                else ()
            ),
            f"reason={','.join(result.reasons)}",
            *(
                (f"{self._overlay.upstream.cid_cell}={upstream_cid}",)
                if upstream_cid and self._overlay.upstream
                else ()
            ),
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
    result: ModelOverlayDriftResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["overlay_drift_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "counts": result.counts.model_dump(mode="json"),
        "failing": [check.model_dump(mode="json") for check in result.failing],
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["overlay_drift_failed"]
            if result.failed
            else topics["overlay_drift_written"],
            body,
        ),
        (
            topics["overlay_drift_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerOverlayDrift, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelOverlayDriftCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["overlay_drift_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerOverlayDrift", "answer_command", "events_of", "load_topics"]
