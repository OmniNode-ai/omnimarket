# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure deterministic tool-reuse matcher handler (OMN-13356).

Routes an incoming tool-generation request to an already-generated tool so the
LLM generation flow can be short-circuited. The match path is explicitly
non-LLM: signature matching is hash equality; the semantic fallback is a
deterministic token-set Jaccard similarity over the task description and each
candidate tool's stored description. No I/O beyond the injected registry query;
the same request against the same registry snapshot always yields the same
result (replay-deterministic).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseMatchStrategy,
    EnumToolReuseVerdict,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_request import (
    ModelToolReuseRequest,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_result import (
    ModelToolReuseCandidate,
    ModelToolReuseMatchResult,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.protocols.protocol_generated_tool_registry import (
    ProtocolGeneratedToolRegistry,
)

# Confidence assigned to an exact input+output signature match. A structural
# signature match is the strongest deterministic reuse signal available.
_SIGNATURE_MATCH_CONFIDENCE = 1.0

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase word-token set for deterministic lexical comparison."""
    return frozenset(_WORD_RE.findall(text.lower()))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-set Jaccard similarity in [0.0, 1.0]. Deterministic, symmetric."""
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


class HandlerToolReuseMatcher:
    """Pure deterministic tool-reuse matcher.

    The registry is injected so the handler has no embedded I/O. The handler
    performs only deterministic comparison; the orchestrator owns publishing the
    resulting ``tool-reuse-matched`` / ``tool-reuse-no-match`` event.
    """

    def __init__(self, registry: ProtocolGeneratedToolRegistry) -> None:
        self._registry = registry

    def handle(
        self, request: ModelToolReuseRequest | Mapping[str, object]
    ) -> ModelToolReuseMatchResult:
        if isinstance(request, Mapping):
            request = ModelToolReuseRequest.model_validate(request)

        try:
            candidates = self._candidates_for_strategy(request)
        except Exception as exc:  # registry query failure — fail loud, not silent
            return ModelToolReuseMatchResult(
                correlation_id=request.correlation_id,
                verdict=EnumToolReuseVerdict.REGISTRY_UNAVAILABLE,
                matched_tool=None,
                candidate_tools=[],
                match_strategy_used=request.match_strategy,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )

        verdict, matched = self._select_verdict(
            candidates, request.similarity_threshold
        )
        return ModelToolReuseMatchResult(
            correlation_id=request.correlation_id,
            verdict=verdict,
            matched_tool=matched,
            candidate_tools=candidates[: request.max_candidates],
            match_strategy_used=request.match_strategy,
            failure_reason=None,
        )

    # ------------------------------------------------------------------ #
    # Strategy dispatch
    # ------------------------------------------------------------------ #
    def _candidates_for_strategy(
        self, request: ModelToolReuseRequest
    ) -> list[ModelToolReuseCandidate]:
        if request.match_strategy == EnumToolReuseMatchStrategy.SIGNATURE:
            return self._match_by_signature(request)
        if request.match_strategy == EnumToolReuseMatchStrategy.SEMANTIC:
            return self._match_by_lexical_similarity(request)
        return self._match_hybrid(request)

    def _match_by_signature(
        self, request: ModelToolReuseRequest
    ) -> list[ModelToolReuseCandidate]:
        sig = request.requested_signature
        records = self._registry.query_by_signature(
            input_fields_hash=sig.input_fields_hash,
            output_fields_hash=sig.output_fields_hash,
        )
        reason = (
            f"Exact input/output signature match "
            f"({sig.input_model_name} -> {sig.output_model_name})"
        )
        return [
            ModelToolReuseCandidate(
                tool=record,
                match_confidence=_SIGNATURE_MATCH_CONFIDENCE,
                match_reason=reason,
            )
            for record in records
        ]

    def _match_by_lexical_similarity(
        self, request: ModelToolReuseRequest
    ) -> list[ModelToolReuseCandidate]:
        request_tokens = _tokenize(request.task_description)
        scored: list[ModelToolReuseCandidate] = []
        for record in self._registry.list_active():
            similarity = _jaccard(
                request_tokens, _tokenize(record.semantic_description)
            )
            scored.append(
                ModelToolReuseCandidate(
                    tool=record,
                    match_confidence=similarity,
                    match_reason=f"Lexical token-set similarity {similarity:.3f} on task description",
                )
            )
        # Highest similarity first; tool_id as a stable deterministic tiebreaker.
        scored.sort(key=lambda c: (c.match_confidence, c.tool.tool_id), reverse=True)
        return scored

    def _match_hybrid(
        self, request: ModelToolReuseRequest
    ) -> list[ModelToolReuseCandidate]:
        # Signature match is the strongest, 0-latency, deterministic signal —
        # prefer it whenever present.
        signature_candidates = self._match_by_signature(request)
        if signature_candidates:
            return signature_candidates
        # Otherwise fall back to lexical similarity over the description.
        return self._match_by_lexical_similarity(request)

    # ------------------------------------------------------------------ #
    # Verdict selection
    # ------------------------------------------------------------------ #
    def _select_verdict(
        self,
        candidates: list[ModelToolReuseCandidate],
        threshold: float,
    ) -> tuple[EnumToolReuseVerdict, ModelToolReuseCandidate | None]:
        qualifying = [c for c in candidates if c.match_confidence >= threshold]
        if not qualifying:
            return EnumToolReuseVerdict.NO_MATCH, None

        top = qualifying[0]
        # A tie at the top score among multiple qualifying candidates is
        # genuinely ambiguous — the caller must rank or escalate. A single
        # strictly-highest candidate is a clean MATCHED.
        tied_at_top = [
            c for c in qualifying if c.match_confidence == top.match_confidence
        ]
        if len(tied_at_top) > 1:
            return EnumToolReuseVerdict.AMBIGUOUS, None
        return EnumToolReuseVerdict.MATCHED, top


__all__ = ["HandlerToolReuseMatcher"]
