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
4. the rows are appended through the ledger's own append command, all of a prompt's rows in one call;
5. with a promotion lane, the words also become one RULING, OPERATOR-CONSENT or orchestrator
   inbox MSG (node_operator_capture_compute's promote_operator_words, joined to the session's
   assistant reply before the prompt), planned and appended with the capture rows (OMN-19513);
6. only when every row is appended is the prompt removed from the inbox and its record written
   to ``captures.jsonl``. A failed append leaves the prompt in the inbox for the next run, which
   is the session-start digest's pending count until it clears.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_capture_health import (
    ModelCaptureHealthFinding,
    ModelCaptureHealthPolicy,
    ModelCaptureHealthRequest,
    ModelClassifierCall,
    ModelClassifierObservation,
)
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
    ModelWordsPromotionRequest,
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
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_words_promotion import (
    HandlerWordsPromotion,
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
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerCaptureHealth,
)


def load_capture_health_policy() -> ModelCaptureHealthPolicy:
    import omnimarket.nodes.node_silent_failure_alarm_compute as alarm_node

    path = Path(alarm_node.__file__).with_name("contract.yaml")
    return ModelCaptureHealthPolicy.model_validate(
        yaml.safe_load(path.read_text())["capture_health"]
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
        health_policy: ModelCaptureHealthPolicy | None = None,
    ) -> None:
        self._delegate = delegate or onex_delegate_runner(None)
        self._append = append or onex_ledger_appender()
        self._now = now
        self._health_policy = health_policy or load_capture_health_policy()

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
        findings: list[ModelCaptureHealthFinding] = []
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
                lane=str(record.get("lane") or "") or None,
            )
            planned = record.get("rows_planned")
            if isinstance(planned, list):
                # A run that appended part of this prompt's rows planned them; the rest go out
                # as planned (restamped to now), never reclassified, so no row is written twice.
                stamp = (self._now or datetime.now(UTC)).strftime(STAMP_FORMAT)
                rows = tuple(_restamp(str(r), stamp) for r in planned)
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
                fresh = self._health_findings(root, classification)
                record["classifier_findings"] = [
                    f.model_dump(mode="json") for f in fresh
                ]
                policy = self._health_policy
                stamp = (self._now or datetime.now(UTC)).strftime(STAMP_FORMAT)
                rows += tuple(
                    f"{stamp} | STATUS | lane={policy.finding_lane} | kind={policy.finding_kind}"
                    f" | alarm={f.alarm} | episode={f.episode} | unanswered={f.unanswered}"
                    f" | capture={utterance.capture_id} | {f.why}; {f.fix}"
                    for f in fresh
                )
            remaining = list(rows)
            failed = False
            if not isinstance(planned, list):
                # Plan before the first append, so a crash after it resumes from the plan and
                # never reclassifies a prompt that already has rows in the ledger.
                record["rows_planned"] = remaining[:]
                record.setdefault("classification", _summary(classification))
                capture_store.rewrite_pending(path, record)
            if remaining:
                # OMN-20275: all of a prompt's rows go as ONE append, so one request carries them to the
                # ledger host and one receipt answers for them; sent one at a time a multi-row ruling
                # overran the append budget and left some rows without their companions.
                ok, detail = self._append("\n".join(remaining))
                if not ok:
                    errors.append(f"{path.name}: {detail}")
                    failed = True
                else:
                    rows_appended += len(remaining)
                    remaining = []
                    record["rows_planned"] = []
                    capture_store.rewrite_pending(path, record)
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
            findings.extend(
                ModelCaptureHealthFinding.model_validate(f)
                for f in record.get("classifier_findings", [])
            )
            processed += 1
        return ModelCaptureProcessResult(
            processed=processed,
            rows_appended=rows_appended,
            machine=machine,
            pending=len(capture_store.pending(root)),
            errors=tuple(errors),
            findings=tuple(findings),
        )

    def _health_findings(
        self, root: Path, classification: ModelUtteranceClassification
    ) -> tuple[ModelCaptureHealthFinding, ...]:
        history = root / "captures.jsonl"
        observations = []
        emitted: set[str] = set()
        if history.exists():
            for line in history.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # an incomplete last journal row is not a classified prompt
                summary = record.get("classification") or {}
                if record.get("status") == "machine" or not summary:
                    continue
                observations.append(
                    ModelClassifierObservation(
                        capture_id=record["capture_id"],
                        classifier=summary["classifier"],
                        call=summary.get("call"),
                    )
                )
                emitted.update(
                    f["episode"] for f in record.get("classifier_findings", [])
                )
        observations.append(
            ModelClassifierObservation(
                capture_id=classification.capture_id,
                classifier=classification.classifier.value,
                call=classification.call,
            )
        )
        result = HandlerCaptureHealth().handle(
            ModelCaptureHealthRequest(
                observations=tuple(observations), policy=self._health_policy
            )
        )
        return tuple(f for f in result.findings if f.episode not in emitted)

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
        call = answer.call or ModelClassifierCall(
            outcome="empty"
            if request.delegate and not answer.response
            else "answered"
            if request.delegate
            else "not-called",
            command="injected delegate port"
            if request.delegate
            else "capture delegate=false",
            observation=answer.error or "delegate port returned a response",
        )
        if (
            answer.response
            and classification.classifier.value
            == self._health_policy.fallback_classifier
        ):
            call = call.model_copy(
                update={"outcome": "invalid", "observation": classification.reason}
            )
        classification = classification.model_copy(update={"call": call})
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
        stamp = self._now or datetime.now(UTC)
        rows = (
            HandlerCaptureRows()
            .handle(
                ModelCaptureRowsRequest(
                    utterance=utterance,
                    classification=classification,
                    drift=drift,
                    stamp=stamp,
                )
            )
            .rows
        )
        if request.promotion_lane and rows:
            # The operator's words also become a RULING, an OPERATOR-CONSENT or an inbox MSG
            # to the orchestrator, planned with the capture rows so a crash writes none twice.
            promotion = HandlerWordsPromotion().handle(
                ModelWordsPromotionRequest(
                    utterance=utterance,
                    classification=classification,
                    drift=drift,
                    reply=capture_store.reply_before(
                        request.store_dir,
                        session_id=utterance.session_id,
                        at=utterance.captured_at,
                    ),
                    lane=request.promotion_lane,
                    stamp=stamp,
                )
            )
            rows += promotion.rows
        return classification, rows


def _restamp(row: str, stamp: str) -> str:
    """The planned row stamped now; a message's id carries its stamp, so it moves with it."""
    old = row[:20]
    out = stamp + row[20:]
    return out.replace(f" | id={old}-", f" | id={stamp}-", 1)


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
        "call": classification.call.model_dump(mode="json")
        if classification.call
        else None,
        "items": [i.model_dump(mode="json") for i in classification.items],
        "drops": list(classification.drops),
    }


__all__ = ["HandlerCaptureProcess", "capture_id_for"]
