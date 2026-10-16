# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for context-pack render tests (OMN-21109, plan task TA.7)."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import EnumContextPackProvenance
from omnibase_core.models.pack.model_context_chunk import ModelContextChunk
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from omnibase_core.utils.util_context_pack import compute_chunk_id

from omnimarket.models.context_pack_render import (
    ModelContextPackCarriedPrompt,
    ModelContextPackRenderChunkRef,
    ModelContextPackRenderIdentity,
    ModelContextPackRenderRequest,
)

LANE_RULES_TEXT = "Never use --no-verify.\nSquash-only."
INVARIANT_TEXT = "INV-005: no new enum where an existing one serves."
RULING_TEXT = (
    '2026-10-15T08:14:15Z | RULING | lane=orchestrator-bb1a | "Also, make sure '
    'we are using canonical shapes for all mod work"  '
)
OPEN_WORK_TEXT = "2026-10-15T14:14:59Z | CLAIM | lane=omn21109-ta7 | ticket=OMN-21109"
WORDS_TEXT = "keep the ledger honest\n\tand don't summarise this"
TAIL_TEXT = "The lane built the renderer."


def chunk(
    factor: EnumContextFactor,
    content: str,
    provenance: EnumContextPackProvenance = EnumContextPackProvenance.CURATED,
) -> ModelContextChunk:
    return ModelContextChunk(
        chunk_id=compute_chunk_id(factor, content),
        factor=factor,
        content=content,
        token_estimate=10,
        token_estimation_method="chars_div_4",
        tokenizer_source="builtin",
        tokenizer_version="1",
        estimation_accuracy="estimated",
        provenance=provenance,
        source_artifact_hash="sha256:" + factor.value,
        source_ticket_id=None,
        source_contract_hash="contract-1",
        source_run_id=None,
    )


def pack(
    chunks: tuple[ModelContextChunk, ...],
    generated_at: str = "2026-10-15T14:14:59Z",
) -> ModelContextPack:
    return ModelContextPack(
        pack_id="pack_0123456789abcdef",
        contract_hash="contract-1",
        model_id="claude-code-session",
        chunks=chunks,
        total_token_estimate=sum(c.token_estimate for c in chunks),
        generated_at=generated_at,
        profile_version="1",
    )


def ref(
    chunk_value: ModelContextChunk,
    source_identifier: str | None = None,
    row_timestamp: str | None = None,
    row_lane: str | None = None,
) -> ModelContextPackRenderChunkRef:
    return ModelContextPackRenderChunkRef(
        chunk_id=chunk_value.chunk_id,
        source_identifier=source_identifier,
        row_timestamp=row_timestamp,
        row_lane=row_lane,
    )


def identity(**overrides: object) -> ModelContextPackRenderIdentity:
    values: dict[str, object] = {
        "candidate_id": "cand-aaaa",
        "assembly_hash": "asm-bbbb",
        "final_pack_hash": "fin-cccc",
        "version_vector_hash": "vec-dddd",
        "profile_name": "compaction_orchestrator",
        "unrecorded_prompt_count": 7,
        "reviewed_prompt_count": 11,
        "scrubbed_span_count": 13,
        "transcript_extract_candidate_count": 17,
    }
    values.update(overrides)
    return ModelContextPackRenderIdentity.model_validate(values)


def prompts() -> tuple[ModelContextPackCarriedPrompt, ...]:
    return (
        ModelContextPackCarriedPrompt(prompt_id="p-1", capture_sequence=1),
        ModelContextPackCarriedPrompt(prompt_id="p-2", capture_sequence=2, cited=True),
        ModelContextPackCarriedPrompt(prompt_id="p-3", capture_sequence=3),
        ModelContextPackCarriedPrompt(
            prompt_id="p-4", capture_sequence=4, in_reviewed_range=True
        ),
        ModelContextPackCarriedPrompt(prompt_id="p-5", capture_sequence=5),
    )


class Fixture:
    """One pack with a stable, a ruling, a state, an unrecorded-words and a model block."""

    def __init__(self) -> None:
        self.lane_rules = chunk(EnumContextFactor.LANE_RULES, LANE_RULES_TEXT)
        self.invariant = chunk(EnumContextFactor.INVARIANTS, INVARIANT_TEXT)
        self.ruling = chunk(
            EnumContextFactor.OPERATOR_RULINGS,
            RULING_TEXT,
            EnumContextPackProvenance.OBSERVED,
        )
        self.open_work = chunk(
            EnumContextFactor.OPEN_WORK,
            OPEN_WORK_TEXT,
            EnumContextPackProvenance.OBSERVED,
        )
        self.words = chunk(EnumContextFactor.OPERATOR_WORDS_UNRECORDED, WORDS_TEXT)
        self.tail = chunk(
            EnumContextFactor.TAIL_SUMMARY,
            TAIL_TEXT,
            EnumContextPackProvenance.GENERATED,
        )
        self.refs = (
            ref(self.lane_rules, "lane_rules/lane-rules-block.md@1111aaa"),
            ref(self.invariant, "reference/invariants.yaml@2222bbb#INV-005:12-14"),
            ref(
                self.ruling,
                row_timestamp="2026-10-15T08:14:15Z",
                row_lane="orchestrator-bb1a",
            ),
        )

    def request(self, **overrides: object) -> ModelContextPackRenderRequest:
        values: dict[str, object] = {
            "pack": pack(
                (
                    self.lane_rules,
                    self.invariant,
                    self.ruling,
                    self.open_work,
                    self.words,
                )
            ),
            "chunk_refs": self.refs,
            "identity": identity(),
            "carried_prompts": prompts(),
        }
        values.update(overrides)
        return ModelContextPackRenderRequest.model_validate(values)
