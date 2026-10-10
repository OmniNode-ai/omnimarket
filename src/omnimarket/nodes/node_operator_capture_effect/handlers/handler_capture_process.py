# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCaptureProcess: turn each inboxed operator prompt into ledger rows (OMN-20905).

One run takes the store's worker lock (a second concurrent run returns at once), then, oldest
first, for each prompt in the inbox:

1. machine injections are recorded locally and removed, with no model call and no row;
2. the delegated local model classifies the message; when it cannot answer, or answers in a
   shape the compute node refuses, the deterministic fallback classifies it instead, so the
   prompt is never lost to a model outage;
3. every decision or preference is checked against the earlier rulings for drift;
4. the rows are appended through the ledger's own append command, one by one;
5. only when every row is appended is the prompt removed from the inbox and its record written
   to ``captures.jsonl``. A failed append leaves the prompt in the inbox for the next run, which
   is the session-start digest's pending count until it clears.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from omnimarket.models.operator_capture import (
    EnumUtteranceKind,
    ModelCaptureProcessRequest,
    ModelCaptureProcessResult,
    ModelCaptureRowsRequest,
    ModelRulingDrift,
    ModelRulingDriftRequest,
    ModelUtterance,
    ModelUtteranceClassification,
    ModelUtteranceClassifyRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_capture_rows import (
    STAMP_FORMAT,
    HandlerCaptureRows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_ruling_drift import (
    HandlerRulingDrift,
    prior_rulings_from_rows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    RESPONSE_CONTRACT,
    HandlerUtteranceClassify,
    build_classify_prompt,
    is_machine_injection,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
    DelegateRunner,
    LedgerAppender,
    onex_delegate_runner,
    onex_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.ledger_read import (
    relevant_rows,
)

_DRIFT_KINDS = frozenset({EnumUtteranceKind.DECISION, EnumUtteranceKind.PREFERENCE})


def capture_id_for(record: dict[str, Any]) -> str:
    key = f"{record.get('session_id')}\0{record.get('digest')}\0{record.get('received_at')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class HandlerCaptureProcess:
    """Process the inbox; injected ports keep the handler testable without a model or ledger."""

    def __init__(
        self,
        delegate: DelegateRunner | None = None,
        append: LedgerAppender | None = None,
        now: datetime | None = None,
    ) -> None:
        self._delegate = delegate or onex_delegate_runner()
        self._append = append or onex_ledger_appender()
        self._now = now

    def handle(self, request: ModelCaptureProcessRequest) -> ModelCaptureProcessResult:
        root = request.store_dir
        with capture_store.worker_lock(root) as held:
            if not held:
                return ModelCaptureProcessResult(
                    pending=len(capture_store.pending(root)),
                    errors=("another capture worker holds the lock",),
                )
            return self._run(request)

    def _run(self, request: ModelCaptureProcessRequest) -> ModelCaptureProcessResult:
        root = request.store_dir
        processed = rows_appended = machine = 0
        errors: list[str] = []
        prior_cache: list[tuple[str, ...]] = []
        for path in capture_store.pending(root)[: request.max_captures]:
            try:
                record = capture_store.read_pending(path)
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}: unreadable ({exc})")
                continue
            text = str(record.get("text", ""))
            utterance = ModelUtterance(
                capture_id=capture_id_for(record),
                text=text,
                session_id=str(record.get("session_id") or "unknown"),
                source=str(record.get("source") or "unknown"),
                captured_at=datetime.fromisoformat(str(record["received_at"])),
            )
            planned = record.get("rows_planned")
            if isinstance(planned, list):
                # A run that appended part of this prompt's rows planned them; the rest go out
                # as planned (restamped to now), never reclassified, so no row is written twice.
                stamp = (self._now or datetime.now(UTC)).strftime(STAMP_FORMAT)
                rows = tuple(stamp + str(r)[20:] for r in planned)
                classification = None
            elif is_machine_injection(text):
                capture_store.record_capture(
                    root,
                    {**record, "capture_id": utterance.capture_id, "status": "machine"},
                )
                path.unlink(missing_ok=True)
                machine += 1
                processed += 1
                continue
            else:
                classification, rows = self._classify(request, utterance, prior_cache)
            remaining = list(rows)
            failed = False
            while remaining:
                ok, detail = self._append(remaining[0])
                if not ok:
                    errors.append(f"{path.name}: {detail}")
                    failed = True
                    break
                remaining.pop(0)
                rows_appended += 1
            if failed:
                record["rows_planned"] = remaining
                record.setdefault("classification", _summary(classification))
                capture_store.rewrite_pending(path, record)
                continue
            summary = _summary(classification) or record.get("classification") or {}
            capture_store.record_capture(
                root,
                {
                    **{k: v for k, v in record.items() if k != "rows_planned"},
                    "capture_id": utterance.capture_id,
                    "status": "recorded",
                    "classification": summary,
                    "rows": list(rows),
                },
            )
            path.unlink(missing_ok=True)
            processed += 1
        return ModelCaptureProcessResult(
            processed=processed,
            rows_appended=rows_appended,
            machine=machine,
            pending=len(capture_store.pending(root)),
            errors=tuple(errors),
        )

    def _classify(
        self,
        request: ModelCaptureProcessRequest,
        utterance: ModelUtterance,
        prior_cache: list[tuple[str, ...]],
    ) -> tuple[ModelUtteranceClassification, tuple[str, ...]]:
        answer = (
            self._delegate(build_classify_prompt(utterance.text), RESPONSE_CONTRACT)
            if request.delegate
            else DelegateAnswer(None, None, "delegation off for this run")
        )
        classification = HandlerUtteranceClassify().handle(
            ModelUtteranceClassifyRequest(
                utterance=utterance,
                delegated_response=answer.response,
                delegated_model=answer.model,
                delegated_error=answer.error,
            )
        )
        drift: dict[str, ModelRulingDrift] = {}
        if any(i.kind in _DRIFT_KINDS for i in classification.items):
            if not prior_cache:
                try:
                    prior_cache.append(relevant_rows(request.ledger_path))
                except OSError:
                    prior_cache.append(())
            prior = prior_rulings_from_rows(prior_cache[0])
            for item in classification.items:
                if item.kind in _DRIFT_KINDS:
                    drift[item.item_id] = HandlerRulingDrift().handle(
                        ModelRulingDriftRequest(
                            item=item, prior=prior, said_at=utterance.captured_at
                        )
                    )
        rows = (
            HandlerCaptureRows()
            .handle(
                ModelCaptureRowsRequest(
                    utterance=utterance,
                    classification=classification,
                    drift=drift,
                    stamp=self._now or datetime.now(UTC),
                )
            )
            .rows
        )
        return classification, rows


def _summary(
    classification: ModelUtteranceClassification | None,
) -> dict[str, Any] | None:
    if classification is None:
        return None
    return {
        "origin": classification.origin.value,
        "classifier": classification.classifier.value,
        "classifier_model": classification.classifier_model,
        "reason": classification.reason,
        "items": [i.model_dump(mode="json") for i in classification.items],
        "drops": list(classification.drops),
    }


__all__ = ["HandlerCaptureProcess", "capture_id_for"]
