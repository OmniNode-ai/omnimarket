# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure deterministic tool-reuse matcher handler (OMN-13356).

Routes an incoming tool-generation request to an already-generated tool so the
LLM generation flow can be short-circuited. The match path is explicitly
non-LLM: signature matching is hash equality; the semantic fallback is a
deterministic token-set Jaccard similarity over the task description and each
candidate tool's stored description. No I/O beyond the resolved registry query;
the same request against the same registry snapshot always yields the same
result (replay-deterministic).

Container-driven pattern (OMN-13603): the handler takes the injectable
``container`` so the runtime resolver constructs it at boot via known-param
injection. The ``ProtocolGeneratedToolRegistry`` is resolved from the container
when a match runs — never stored at construction — so an unregistered registry
no longer quarantines the handler at boot. The match comparison itself stays
pure and deterministic over the registry snapshot the container provides.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

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

    The registry is resolved from the injected ``container`` at match time so
    the handler has no embedded I/O and no construction-time dependency. The
    handler performs only deterministic comparison; the orchestrator owns
    publishing the resulting ``tool-reuse-matched`` / ``tool-reuse-no-match``
    event.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def _resolve_registry(self) -> ProtocolGeneratedToolRegistry:
        """Resolve ProtocolGeneratedToolRegistry from the container.

        Resolution failure surfaces as ``REGISTRY_UNAVAILABLE`` (handled by the
        caller's try/except) rather than crashing dispatch — an absent registry
        is a recoverable verdict, not a handler defect.
        """
        # NOTE(OMN-13603): mypy false-positive — a Protocol is the canonical DI
        # key for get_service; it is never instantiated here.
        return self._container.get_service(
            ProtocolGeneratedToolRegistry  # type: ignore[type-abstract]  # Protocol used as DI key
        )

    def handle(
        self, request: ModelToolReuseRequest | Mapping[str, object]
    ) -> ModelToolReuseMatchResult:
        if isinstance(request, Mapping):
            request = ModelToolReuseRequest.model_validate(request)

        try:
            registry = self._resolve_registry()
            candidates = self._candidates_for_strategy(request, registry)
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
        self,
        request: ModelToolReuseRequest,
        registry: ProtocolGeneratedToolRegistry,
    ) -> list[ModelToolReuseCandidate]:
        if request.match_strategy == EnumToolReuseMatchStrategy.SIGNATURE:
            return self._match_by_signature(request, registry)
        if request.match_strategy == EnumToolReuseMatchStrategy.SEMANTIC:
            return self._match_by_lexical_similarity(request, registry)
        return self._match_hybrid(request, registry)

    def _match_by_signature(
        self,
        request: ModelToolReuseRequest,
        registry: ProtocolGeneratedToolRegistry,
    ) -> list[ModelToolReuseCandidate]:
        sig = request.requested_signature
        records = registry.query_by_signature(
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
        self,
        request: ModelToolReuseRequest,
        registry: ProtocolGeneratedToolRegistry,
    ) -> list[ModelToolReuseCandidate]:
        request_tokens = _tokenize(request.task_description)
        scored: list[ModelToolReuseCandidate] = []
        for record in registry.list_active():
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
        self,
        request: ModelToolReuseRequest,
        registry: ProtocolGeneratedToolRegistry,
    ) -> list[ModelToolReuseCandidate]:
        # Signature match is the strongest, 0-latency, deterministic signal —
        # prefer it whenever present.
        signature_candidates = self._match_by_signature(request, registry)
        if signature_candidates:
            return signature_candidates
        # Otherwise fall back to lexical similarity over the description.
        return self._match_by_lexical_similarity(request, registry)

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
