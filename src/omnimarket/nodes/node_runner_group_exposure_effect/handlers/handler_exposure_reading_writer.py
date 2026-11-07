# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerExposureReadingWriter: run the exposure probe and append one reading row for the run.

EFFECT handler: it writes a reading row and nothing else. A reading is never a gate, a stop or an
admission. A group that cannot be read is an UNKNOWN row (outcome ``source-unreadable``); a
NOT_WORKING verdict that was read is a reading, not a failure; an append that did not land is a
failure. The appender, the host and the probe are injected, so the in-memory overlay and a fake
appender run the whole chain with no network and no ledger.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_reading import EnumReadingOutcome
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_reading import (
    ModelExposureReadingCommand,
    ModelExposureReadingOutcome,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelExposureReadingOverlay,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=runner-group-exposure"


def load_reading_topics() -> dict[str, str]:
    """The topics by role, read from the contract (the handler holds no topic string)."""
    return dict(yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerExposureReadingWriter:
    def __init__(
        self,
        *,
        overlay: ModelExposureReadingOverlay,
        host: str,
        probe: HandlerExposureProbe,
        appender: ProtocolReadingAppender | None,
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._probe = probe
        self._appender = appender

    def handle(
        self, request: ModelExposureReadingCommand
    ) -> ModelExposureReadingOutcome:
        overlay = self._overlay
        result = self._probe.handle(
            ModelExposureProbeRequest(
                correlation_id=request.correlation_id, now=request.now
            )
        )
        reading = result.reading
        failure = reading.read_failure
        outcome = (
            EnumReadingOutcome.SOURCE_UNREADABLE
            if failure is not None
            else EnumReadingOutcome.READ
        )
        reasons = (
            (reason_token(failure.value),)
            if failure is not None
            else tuple(
                reason_token(f"{s.name}-{s.reason}")
                for s in reading.subjects
                if s.verdict.value != "WORKING"
            )
        )
        subject_cells = [f"{s.name}={s.verdict.value}" for s in reading.subjects]
        subject_counts = [
            f"{s.name}_count={s.count}" for s in reading.subjects if s.count is not None
        ]
        overlay_hash = overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(request.now), overlay_hash)))[:32]
        content = _sha(
            json.dumps(
                [
                    reading.verdict.value,
                    outcome.value,
                    list(reasons),
                    subject_cells,
                    subject_counts,
                ],
                sort_keys=True,
            )
        )[:32]

        def done(ledger: str, row: str | None = None) -> ModelExposureReadingOutcome:
            return ModelExposureReadingOutcome(
                correlation_id=request.correlation_id,
                host=self._host,
                backend=result.backend,
                verdict=reading.verdict,
                outcome=outcome,
                reasons=reasons,
                calls=result.calls,
                row_key=key,
                ledger=ledger,
                row=row,
            )

        if not request.write:
            return done("not-written")
        if self._appender is None:
            return done("append-failed:no-appender")
        publish = overlay.publish
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"backend={result.backend}",
            f"verdict={reading.verdict.value}",
            f"outcome={outcome.value}",
            f"reason={','.join(reasons)}",
            *subject_cells,
            *subject_counts,
            f"calls={result.calls}",
            f"probed_at={stamp_text(request.now)}",
            f"published_at={stamp_text(request.now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            _clean(f"injected fault: {publish.injected_fault}"),
        ]
        row = f"{stamp_text(request.now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return done(f"append-failed:{appended.status}", row)
        return done(
            "written" if appended.status == "accepted" else "already-written", row
        )

    @staticmethod
    def event_for(
        outcome: ModelExposureReadingOutcome,
    ) -> tuple[str, dict[str, Any]]:
        """The terminal event of a run: the failure topic when it failed."""
        topics = load_reading_topics()
        topic = (
            topics["reading_failed"] if outcome.failed else topics["reading_written"]
        )
        body = outcome.model_dump(mode="json", exclude={"row"})
        return topic, body


__all__ = ["HandlerExposureReadingWriter", "load_reading_topics"]
