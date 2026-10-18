# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextPackRender: ModelContextPack -> ordered role/text messages (OMN-21109, TA.7).

Pure and definition-B: ``handle(ModelContextPackRenderRequest) -> ModelContextPackRenderResult``,
no event envelope. The message order is fixed by plan section 4.6: the fixed legend, the
stable factors (plan section 4.2 rows 1 to 5), the volatile factors, the model-written
factors, then the identity trailer. Per-build values (candidate id, hashes, counts,
``generated_at``) appear in the trailer only; a stable block keeps its immutable source
identifier. The same request renders the same bytes.
"""

from __future__ import annotations

import hashlib
import json

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.pack.model_context_chunk import ModelContextChunk

from omnimarket.models.context_pack_render import (
    EnumContextPackRenderFailure,
    EnumContextPackRenderStatus,
    ModelContextPackRenderMessage,
    ModelContextPackRenderRequest,
    ModelContextPackRenderResult,
)

RENDER_CONTRACT_VERSION = "context-pack-render/1"
_ROLE = "user"

_LEDGER_ROW = "ledger row"
_OPERATOR_QUOTE = "operator quote"
_DOCUMENT_EXCERPT = "registry or document excerpt"
_OBSERVED_STATE = "observed state"
_VERIFIED_EXTRACT = "verified extract"
_MODEL_OUTPUT = "model output"

_PROVENANCE: dict[EnumContextFactor, str] = {
    EnumContextFactor.LANE_RULES: _DOCUMENT_EXCERPT,
    EnumContextFactor.INVARIANTS: _DOCUMENT_EXCERPT,
    EnumContextFactor.ADRS: _DOCUMENT_EXCERPT,
    EnumContextFactor.CANONICAL_SHAPES: _DOCUMENT_EXCERPT,
    EnumContextFactor.ARCHITECTURE_PATTERNS: _DOCUMENT_EXCERPT,
    EnumContextFactor.EXEMPLAR: _DOCUMENT_EXCERPT,
    EnumContextFactor.PLAN_EXCERPTS: _DOCUMENT_EXCERPT,
    EnumContextFactor.CLAUDE_MD: _DOCUMENT_EXCERPT,
    EnumContextFactor.OPERATOR_RULINGS: _LEDGER_ROW,
    EnumContextFactor.OPEN_WORK: _LEDGER_ROW,
    EnumContextFactor.OPERATOR_WORDS_UNRECORDED: _OPERATOR_QUOTE,
    EnumContextFactor.CONVERSATION_TAIL: _OPERATOR_QUOTE,
    EnumContextFactor.ORCHESTRATION_STATE: _OBSERVED_STATE,
    EnumContextFactor.REPO_STATE: _OBSERVED_STATE,
    EnumContextFactor.SESSION_GOAL: _OBSERVED_STATE,
    EnumContextFactor.CAPABILITY_DIGEST: _OBSERVED_STATE,
    EnumContextFactor.GOLDEN_CHAIN: _OBSERVED_STATE,
    EnumContextFactor.LOCAL_FAILURES: _OBSERVED_STATE,
    EnumContextFactor.TRANSCRIPT_EXTRACTS: _VERIFIED_EXTRACT,
    EnumContextFactor.TAIL_SUMMARY: _MODEL_OUTPUT,
}

# Plan section 4.2 rows 1 to 5: bytes depend only on the pinned commit and the selection.
_STABLE_FACTORS = frozenset(
    {
        EnumContextFactor.LANE_RULES,
        EnumContextFactor.INVARIANTS,
        EnumContextFactor.ADRS,
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.EXEMPLAR,
    }
)
_MODEL_WRITTEN_FACTORS = frozenset({EnumContextFactor.TAIL_SUMMARY})

_LEGEND = "\n".join(
    [
        f"Context pack, render contract {RENDER_CONTRACT_VERSION}.",
        "Each block opens with a header, [factor: provenance] and the block's source.",
        "Provenance labels:",
        f"- {_LEDGER_ROW}: a work-ledger row quoted in full, referenced by its "
        "timestamp and lane.",
        f"- {_OPERATOR_QUOTE}: operator words verbatim as captured.",
        f"- {_DOCUMENT_EXCERPT}: text copied verbatim from a pinned source and cited "
        "path@sha.",
        f"- {_OBSERVED_STATE}: state read from a system at a pinned point, not authored.",
        f"- {_VERIFIED_EXTRACT}: a span a model proposed and a deterministic verifier "
        "admitted as a verbatim quote of captured operator words; a candidate, never "
        "a ruling.",
        f"- {_MODEL_OUTPUT}: text a model wrote; not verbatim and not evidence.",
        "Blocks run stable first, then volatile, then model output. "
        "The last block is the identity trailer.",
    ]
)


def provenance_label(factor: EnumContextFactor) -> str | None:
    """The legend label of a factor, or None for a factor this renderer does not know."""
    return _PROVENANCE.get(factor)


def is_stable_factor(factor: EnumContextFactor) -> bool:
    """True for the factors that form the cached prefix."""
    return factor in _STABLE_FACTORS


def _hash_messages(messages: list[ModelContextPackRenderMessage]) -> str:
    raw = json.dumps(
        [m.model_dump() for m in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _failure(
    failure_class: EnumContextPackRenderFailure, error: str
) -> ModelContextPackRenderResult:
    return ModelContextPackRenderResult(
        status=EnumContextPackRenderStatus.FAILED,
        failure_class=failure_class,
        errors=(error,),
    )


class HandlerContextPackRender:
    """Render a ModelContextPack into the stable-first message list and its hashes."""

    def handle(
        self, request: ModelContextPackRenderRequest
    ) -> ModelContextPackRenderResult:
        refs = {ref.chunk_id: ref for ref in request.chunk_refs}
        pack_ids = {chunk.chunk_id for chunk in request.pack.chunks}
        strays = sorted(set(refs) - pack_ids)
        if strays:
            return _failure(
                EnumContextPackRenderFailure.UNKNOWN_CHUNK_REF,
                "chunk ref names no chunk of the pack: " + ", ".join(strays),
            )

        stable: list[ModelContextPackRenderMessage] = []
        volatile: list[ModelContextPackRenderMessage] = []
        model_written: list[ModelContextPackRenderMessage] = []
        for chunk in request.pack.chunks:
            label = provenance_label(chunk.factor)
            if label is None:
                return _failure(
                    EnumContextPackRenderFailure.UNCLASSIFIED_FACTOR,
                    f"factor {chunk.factor.value} has no provenance label",
                )
            ref = refs.get(chunk.chunk_id)
            if chunk.factor is EnumContextFactor.OPERATOR_RULINGS:
                if ref is None or ref.row_timestamp is None or ref.row_lane is None:
                    return _failure(
                        EnumContextPackRenderFailure.MISSING_RULING_REFERENCE,
                        f"ruling chunk {chunk.chunk_id} lacks its row timestamp or lane",
                    )
                source = f"{ref.row_timestamp} lane={ref.row_lane}"
            elif is_stable_factor(chunk.factor):
                if ref is None or ref.source_identifier is None:
                    return _failure(
                        EnumContextPackRenderFailure.MISSING_SOURCE_IDENTIFIER,
                        f"stable chunk {chunk.chunk_id} ({chunk.factor.value}) "
                        "lacks its source identifier",
                    )
                source = ref.source_identifier
            else:
                source = (
                    ref.source_identifier
                    if ref is not None and ref.source_identifier is not None
                    else chunk.source_artifact_hash
                )
            message = _block(chunk, label, source)
            if is_stable_factor(chunk.factor):
                stable.append(message)
            elif chunk.factor in _MODEL_WRITTEN_FACTORS:
                model_written.append(message)
            else:
                volatile.append(message)

        if model_written and request.identity.compact_route_identity is None:
            return _failure(
                EnumContextPackRenderFailure.MODEL_ROUTE_IDENTITY_MISSING,
                "the pack holds a model-written block and the identity names no route",
            )

        prefix = [_message(_LEGEND), *stable]
        messages = [*prefix, *volatile, *model_written, _message(_trailer(request))]
        return ModelContextPackRenderResult(
            status=EnumContextPackRenderStatus.OK,
            messages=tuple(messages),
            body_hash=_hash_messages(messages),
            stable_prefix_hash=_hash_messages(prefix),
            stable_prefix_message_count=len(prefix),
        )


def _message(text: str) -> ModelContextPackRenderMessage:
    return ModelContextPackRenderMessage(role=_ROLE, text=text)


def _block(
    chunk: ModelContextChunk, label: str, source: str
) -> ModelContextPackRenderMessage:
    return _message(f"[{chunk.factor.value}: {label}] {source}\n{chunk.content}")


def _trailer(request: ModelContextPackRenderRequest) -> str:
    identity = request.identity
    uncited = [
        prompt.prompt_id
        for prompt in sorted(
            request.carried_prompts, key=lambda p: (p.capture_sequence, p.prompt_id)
        )
        if not prompt.cited and not prompt.in_reviewed_range
    ]
    lines = [
        "identity trailer",
        f"candidate_id: {identity.candidate_id}",
        f"assembly_hash: {identity.assembly_hash}",
        f"final_pack_hash: {identity.final_pack_hash}",
        f"version_vector_hash: {identity.version_vector_hash}",
        f"profile: {identity.profile_name}@{request.pack.profile_version}",
        f"generated_at: {request.pack.generated_at}",
        f"unrecorded_prompts: {identity.unrecorded_prompt_count}",
        f"reviewed_prompts: {identity.reviewed_prompt_count}",
        f"scrubbed_spans: {identity.scrubbed_span_count}",
        f"transcript_extract_candidates: {identity.transcript_extract_candidate_count}",
    ]
    if any(chunk.factor in _MODEL_WRITTEN_FACTORS for chunk in request.pack.chunks):
        lines.append(f"compact_route: {identity.compact_route_identity}")
    lines.append("uncited_prompt_ids: " + (", ".join(uncited) if uncited else "none"))
    return "\n".join(lines)


__all__ = [
    "RENDER_CONTRACT_VERSION",
    "HandlerContextPackRender",
    "is_stable_factor",
    "provenance_label",
]
