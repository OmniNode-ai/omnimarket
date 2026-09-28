# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPromptIntentClassify: captured prompt in, intent-classified event out (OMN-19552).

WHY THIS NODE EXISTS. The only publisher of intent-classified.v1 was
omniintelligence ``node_claude_hook_event_effect``. The runtime image removes
omniintelligence's node and plugin entry points (``strip_runtime_entry_points``
in omnibase_infra ``docker/Dockerfile.runtime``, the market-only discovery
surface), so that node is discovered by no lab runtime: on 2026-09-25 the .201
dev bus had no consumer group on claude-hook-event.v1 and
``intent_classification_events`` held 0 rows. Its input had also stopped
carrying the prompt: the registered UserPromptSubmit hook sends a length only.
Full prompts now travel on content-captured.v1 (OMN-19550), scrubbed by the
capture-redaction contract, and this node reads them there.

WHY IT IS A COMPUTE NODE. The decision is a pure function of the prompt text:
TF-IDF scoring plus the typed 8-class mapping. The classifier itself stays in
omniintelligence and is called as a library, the same way
``node_dispatch_outcome_bridge_effect`` calls its evaluator. omniintelligence
is a runtime peer installed in the image; it cannot be a declared dependency
(it depends on omnimarket), so the import is deferred to first use.
The first import is slow (about 13 s measured in the dev runtime on
2026-09-25, most of it transformers pulled in by the classifier package) and
is paid once per process.

WHAT IT SKIPS, returning no event: records that are not prompts, every chunk
after the first (the first chunk is the start of the prompt, which is what
the classifier weighs), blank content, and records with no correlation id
(the projection upserts on it, so an event without one cannot land).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from omnimarket.nodes.node_prompt_intent_classify_compute.models.model_content_captured_record import (
    ModelContentCapturedRecord,
)
from omnimarket.nodes.node_prompt_intent_classify_compute.models.model_intent_classified import (
    ModelIntentClassified,
)
from omnimarket.nodes.node_prompt_intent_classify_compute.models.model_prompt_intent_verdict import (
    ModelPromptIntentVerdict,
)

PROMPT_CONTENT_KIND = "prompt"
DEFAULT_AGENT_SOURCE = "claude"

PromptClassifier = Callable[[str], ModelPromptIntentVerdict]


@cache
def _omniintelligence_functions() -> tuple[
    Callable[..., object], Callable[..., object]
]:
    from omniintelligence.nodes.node_intent_classifier_compute.handlers.handler_intent_classification import (  # type: ignore[import-not-found]
        classify_intent,
    )
    from omniintelligence.nodes.node_intent_classifier_compute.handlers.handler_typed_classification import (  # type: ignore[import-not-found]
        resolve_typed_intent,
    )

    return classify_intent, resolve_typed_intent


def classify_with_omniintelligence(prompt: str) -> ModelPromptIntentVerdict:
    """Classify one prompt with the omniintelligence TF-IDF classifier."""
    classify_intent, resolve_typed_intent = _omniintelligence_functions()
    raw = classify_intent(prompt)
    if not isinstance(raw, dict):
        msg = f"classify_intent returned {type(raw).__name__}, expected a mapping"
        raise TypeError(msg)
    category = str(raw.get("intent_category", "unknown"))
    confidence = float(raw.get("confidence", 0.0))
    keywords = [str(k) for k in raw.get("keywords", []) or []]
    typed = resolve_typed_intent(category, confidence)
    intent_class = getattr(typed, "intent_class", None)
    class_value = getattr(intent_class, "value", intent_class)
    return ModelPromptIntentVerdict(
        intent_category=category,
        intent_class=str(class_value),
        confidence=confidence,
        keywords=keywords,
    )


def _agent_source(actor: str | None) -> str:
    if not actor or not actor.strip():
        return DEFAULT_AGENT_SOURCE
    return actor.strip().split(":", 1)[0].lower()


class HandlerPromptIntentClassify:
    """Classify the intent of a captured prompt. No I/O besides the classifier call."""

    def __init__(self, classifier: PromptClassifier | None = None) -> None:
        self._classifier: PromptClassifier = (
            classifier if classifier is not None else classify_with_omniintelligence
        )

    def handle(
        self, payload: ModelContentCapturedRecord
    ) -> ModelIntentClassified | None:
        """Return the intent-classified event for a prompt record, else ``None``.

        Args:
            payload: one content-captured record. Named ``payload`` so the
                RuntimeLocal adapter passes the validated record positionally
                (the OMN-13276 shape).
        """
        record = payload
        if record.content_kind != PROMPT_CONTENT_KIND:
            return None
        if record.chunk_index != 0:
            return None
        if not record.correlation_id:
            return None
        text = record.content or ""
        if not text.strip():
            return None

        verdict = self._classifier(text)
        return ModelIntentClassified(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            intent_class=verdict.intent_class,
            intent_category=verdict.intent_category,
            confidence=verdict.confidence,
            keywords=list(verdict.keywords),
            emitted_at=record.emitted_at,
            agent_source=_agent_source(record.actor),
        )


__all__ = [
    "HandlerPromptIntentClassify",
    "PromptClassifier",
    "classify_with_omniintelligence",
]
