# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retained stores of the job-start source (OMN-22897).

``InMemoryJobStartStore`` keeps the typed results (the default way to run). ``LedgerJobStartStore``
appends one STATUS row per read through the appender the reading writer uses, so the read is kept
where the runner-group read keeps its own. Both keep one read per row key: the same key again is
reported ``already-written``. A failed read is kept too, as an unread record that holds no starts.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from omnimarket.models.model_reading import EnumReadingOutcome
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    ModelJobStartResult,
    ModelJobStartStoreOutcome,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import ModelLedgerStore
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

_ROW_TYPE = "STATUS"
_PUBLIC_SUBJECT = "js_public_starts_off_baseline"
#: Public run ids a row names, at most: the row names the runs to reconcile, not every run.
_RUN_IDS_IN_ROW = 20


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def starts_digest(result: ModelJobStartResult) -> str:
    """A digest of the starts read: repository, run, job and start time, in start order."""
    return _sha(
        json.dumps(
            [
                [s.repository, s.run_id, s.job_id, s.started_at.isoformat()]
                for s in result.starts
            ]
        )
    )[:24]


class InMemoryJobStartStore:
    def __init__(self) -> None:
        self.results: list[ModelJobStartResult] = []

    def append(self, result: ModelJobStartResult) -> ModelJobStartStoreOutcome:
        if any(kept.row_key == result.row_key for kept in self.results):
            return ModelJobStartStoreOutcome(status="already-written")
        self.results.append(result)
        return ModelJobStartStoreOutcome(status="written")


class LedgerJobStartStore:
    def __init__(
        self,
        appender: ProtocolReadingAppender | None,
        settings: ModelLedgerStore,
        host: str,
    ) -> None:
        self._appender = appender
        self._s = settings
        self._host = host

    def row_for(
        self, result: ModelJobStartResult, overlay_hash: str | None = None
    ) -> str:
        s = self._s
        failure = result.read_failure
        outcome = (
            EnumReadingOutcome.SOURCE_UNREADABLE
            if failure is not None
            else EnumReadingOutcome.READ
        )
        reasons = (
            (reason_token(failure.value),)
            if failure is not None
            else tuple(
                reason_token(f"{x.name}-{x.reason}")
                for x in result.reading.subjects
                if x.verdict.value != "WORKING"
            )
        )
        subject_cells = [f"{x.name}={x.verdict.value}" for x in result.reading.subjects]
        subject_counts = [
            f"{x.name}_count={x.count}"
            for x in result.reading.subjects
            if x.count is not None
        ]
        cells = [
            f"lane={s.lane}",
            "src=job-start-source",
            f"actor={s.actor}",
            "model=none",
            f"host={self._host}",
            f"backend={result.backend}",
            f"verdict={result.reading.verdict.value}",
            f"outcome={outcome.value}",
            f"reason={','.join(reasons)}",
            *subject_cells,
            *subject_counts,
            f"repositories={len(result.repositories_read)}/{len(result.declared_repositories)}",
            f"window_start={stamp_text(result.window_start)}",
            f"window_end={stamp_text(result.window_end)}",
        ]
        if failure is None:
            cells.append(f"job_starts={len(result.starts)}")
            if result.starts:
                cells.append(
                    f"first_started_at={stamp_text(result.starts[0].started_at)}"
                )
                cells.append(
                    f"last_started_at={stamp_text(result.starts[-1].started_at)}"
                )
            cells.append(f"starts_digest={starts_digest(result)}")
            public = next(
                (x for x in result.reading.subjects if x.name == _PUBLIC_SUBJECT), None
            )
            if public is not None and public.run_ids:
                cells.append(
                    "public_off_baseline_runs="
                    + ",".join(str(i) for i in public.run_ids[:_RUN_IDS_IN_ROW])
                )
        else:
            cells.append(
                f"failed_repository={_clean(result.failed_repository or 'none')}"
            )
        content = _sha(
            json.dumps(
                [
                    result.reading.verdict.value,
                    outcome.value,
                    list(reasons),
                    subject_cells,
                    subject_counts,
                    starts_digest(result),
                ],
                sort_keys=True,
            )
        )[:32]
        cells += [
            f"calls={result.calls}",
            f"probed_at={stamp_text(result.window_end)}",
            f"published_at={stamp_text(result.window_end)}",
            f"max_age={s.publish_max_age_seconds}",
            f"key={result.row_key}",
            f"content={content}",
            _clean(f"injected fault: {s.injected_fault}"),
        ]
        return f"{stamp_text(result.window_end)} | {_ROW_TYPE} | " + " | ".join(cells)

    def append(self, result: ModelJobStartResult) -> ModelJobStartStoreOutcome:
        if self._appender is None:
            return ModelJobStartStoreOutcome(status="failed", detail="no-appender")
        if not self._host:
            return ModelJobStartStoreOutcome(status="failed", detail="no-host")
        row = self.row_for(result)
        request_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{self._host}|{result.row_key}|{_sha(row)}")
        )
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return ModelJobStartStoreOutcome(
                status="failed", detail=f"append-failed:{appended.status}"
            )
        return ModelJobStartStoreOutcome(
            status="written" if appended.status == "accepted" else "already-written"
        )


class NullJobStartStore:
    """Keeps nothing: a probe reads and judges and writes no record."""

    def append(self, result: ModelJobStartResult) -> ModelJobStartStoreOutcome:
        return ModelJobStartStoreOutcome(status="not-written")
