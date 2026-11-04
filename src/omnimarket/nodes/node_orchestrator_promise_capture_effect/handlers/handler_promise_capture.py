# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop records into a durable, replay-idempotent promise projection."""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC
from typing import Literal

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarReport,
    ModelPromiseCaptureReceipt,
    ModelPromiseCaptureRecord,
    ModelPromiseClassifyRequest,
    ModelPromiseDelegateAnswer,
    ModelPromiseEffectOverlay,
    ModelPromiseStoredCapture,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.protocols import (
    ProtocolPromiseClassifier,
    ProtocolPromiseStore,
)
from omnimarket.protocols.protocol_promise_delegate import ProtocolPromiseDelegate


def _cell(text: str) -> str:
    return " ".join(re.sub(r"[|=\r\n]", " ", text).split())


def _words(text: str) -> str:
    # JSON encoding preserves exact text while keeping one physical ledger row.
    return json.dumps(text, ensure_ascii=False).replace("|", "\\u007c")


def capture_rows(
    capture: ModelPromiseStoredCapture, overlay: ModelPromiseEffectOverlay
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    utterance, classified, policy = (
        capture.utterance,
        capture.classification,
        overlay.policy,
    )
    if classified.machine:
        return (), ()
    stamp = utterance.captured_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    common = [
        f"lane={_cell(policy.row_lane)}",
        f"actor={_cell(policy.row_actor)}",
        f"capture={_cell(utterance.capture_id)}",
        f"session={_cell(utterance.session_id)}",
        f"source={_cell(utterance.source)}",
        f"said={stamp}",
        f"owner={_cell(utterance.lane or policy.default_owner)}",
    ]
    rows: list[str] = []
    ledger: list[str] = []
    for item in classified.items:
        row = " | ".join(
            [
                stamp,
                "STATUS",
                *common,
                f"kind={item.kind}",
                f"item={item.item_id}",
                f"state={item.state}",
                f"confidence={item.confidence:.4f}",
                f"classifier={classified.classifier.value}",
                f"subject={_cell(item.subject)}",
                _words(item.quote),
            ]
        )
        rows.append(row)
        if item.state == "open":
            ledger.append(row)
    for disposition in classified.dispositions:
        rows.append(
            " | ".join(
                [
                    stamp,
                    "STATUS",
                    *common,
                    f"kind={_cell(policy.disposition_kind)}",
                    f"sentence={disposition.sentence}",
                    f"item={disposition.item_id or policy.chat_item}",
                    _words(disposition.quote),
                ]
            )
        )
    return tuple(rows), tuple(ledger)


class HandlerPromiseCapture:
    def __init__(
        self,
        *,
        store: ProtocolPromiseStore | None = None,
        classifier: ProtocolPromiseClassifier | None = None,
        delegate: ProtocolPromiseDelegate | None = None,
        overlay: ModelPromiseEffectOverlay | None = None,
        evaluation: ModelPromiseBarReport | None = None,
    ) -> None:
        if store is None and classifier is None and overlay is None:
            from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
                dependencies_from_overlay,
            )

            resolved = dependencies_from_overlay()
            store, classifier, overlay = resolved
        if store is None or classifier is None or overlay is None:
            raise ValueError(
                "promise store, classifier and overlay must be bound together"
            )
        self.store = store
        self._classifier = classifier
        self._overlay = overlay
        self._delegate = delegate
        self._evaluation = evaluation
        self._lock = threading.RLock()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _receipt(
        self,
        status: Literal["recorded", "duplicate", "pending", "ignored"],
        capture_id: str | None = None,
        rows: tuple[str, ...] = (),
        ledger_rows: tuple[str, ...] = (),
    ) -> ModelPromiseCaptureReceipt:
        captures = self.store.captures()
        return ModelPromiseCaptureReceipt(
            status=status,
            capture_id=capture_id,
            rows=rows,
            ledger_rows=ledger_rows,
            candidate_count=sum(
                item.state == "candidate"
                for capture in captures
                for item in capture.classification.items
            ),
            low_confidence_count=sum(
                capture.classification.low_confidence for capture in captures
            ),
        )

    def handle(self, request: ModelPromiseCaptureRecord) -> ModelPromiseCaptureReceipt:
        owner = self._overlay.policy.session_lanes.get(request.session_id, request.lane)
        if (
            request.content_kind != self._overlay.assistant_reply_kind
            or request.agent_id
            or not owner
            or re.search(self._overlay.policy.orchestrator_lane_pattern, owner) is None
        ):
            return self._receipt("ignored")
        request = request.model_copy(update={"lane": owner})
        with self._lock:
            capture_id, utterance = self.store.assemble(request)
            if utterance is None:
                return self._receipt("pending", capture_id)
            if self.store.get(capture_id) is not None:
                return self._receipt("duplicate", capture_id)
            classify_request = ModelPromiseClassifyRequest(
                utterance=utterance,
                policy=self._overlay.policy,
                evaluation=self._evaluation,
            )
            if self._delegate is not None:
                try:
                    answer = self._delegate.classify(
                        utterance, ModelPromiseDelegateAnswer.model_json_schema()
                    )
                except (OSError, RuntimeError, TimeoutError) as exc:
                    classify_request = classify_request.model_copy(
                        update={"delegated_error": type(exc).__name__}
                    )
                else:
                    classify_request = classify_request.model_copy(
                        update={
                            "delegated_response": answer.response,
                            "delegated_model": answer.model,
                            "delegated_error": answer.error,
                        }
                    )
            classified = self._classifier.handle(classify_request)
            if classified.machine:
                return self._receipt("ignored", capture_id)
            capture = ModelPromiseStoredCapture(
                utterance=utterance, classification=classified, rows=(), ledger_rows=()
            )
            rows, ledger = capture_rows(capture, self._overlay)
            capture = capture.model_copy(update={"rows": rows, "ledger_rows": ledger})
            if not self.store.put(capture):
                return self._receipt("duplicate", capture_id)
            return self._receipt("recorded", capture_id, rows, ledger)
