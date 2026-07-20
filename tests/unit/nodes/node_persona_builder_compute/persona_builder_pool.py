# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic candidate input pool for node_persona_builder_compute (OMN-14836).

Single source of truth for the reviewed scenario pool shared by:

* ``test_persona_builder_golden_equivalence.py`` — records/replays goldens; and
* the adequacy-receipt + hand-flip proof under ``scripts/ci/adequacy_receipts/``.

Every input is fully deterministic (fixed ``signal_id`` UUIDs + fixed ``emitted_at``
/ ``created_at``) so a candidate's canonical ``input_hash`` is stable across runs —
the adequacy receipt and the hand-flip parity block are bound to the SAME hashes.

``persona.created_at`` is the ONLY non-deterministic OUTPUT field (set to
``datetime.now`` inside the pure reducer), so ``normalize_output`` masks it before
the golden fingerprint is taken; it is declared in the receipt ``volatile_mask``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
from omnimemory.models.persona import ModelPersonaSignal, ModelUserPersonaV1

from omnimarket.nodes.node_persona_builder_compute.models.model_classify_request import (
    ModelPersonaClassifyRequest,
)

# Fixed instant for every emitted_at / created_at that is not order-sensitive.
_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
# Sentinel that replaces the volatile persona.created_at in golden comparisons.
CREATED_AT_SENTINEL = "1970-01-01T00:00:00+00:00"


def _sig(
    n: int,
    signal_type: str,
    inferred_value: str,
    confidence: float = 0.9,
    *,
    second: int = 0,
) -> ModelPersonaSignal:
    """Deterministic signal: ``signal_id`` from ``n``, ``emitted_at`` from ``second``."""
    return ModelPersonaSignal(
        signal_id=UUID(int=n),
        user_id="user-1",
        session_id="sess-1",
        signal_type=signal_type,
        evidence="observed behavior",
        inferred_value=inferred_value,
        confidence=confidence,
        emitted_at=_T0.replace(second=second),
    )


def _profile(
    *,
    technical_level: EnumTechnicalLevel,
    vocabulary_complexity: float,
    preferred_tone: EnumPreferredTone,
    session_count: int,
    persona_version: int,
) -> ModelUserPersonaV1:
    return ModelUserPersonaV1(
        user_id="user-1",
        technical_level=technical_level,
        vocabulary_complexity=vocabulary_complexity,
        preferred_tone=preferred_tone,
        domain_familiarity={},
        session_count=session_count,
        persona_version=persona_version,
        created_at=_T0,
        rebuilt_from_signals=session_count,
    )


def build_candidate_pool() -> list[ModelPersonaClassifyRequest]:
    """Reviewed, branch-covering, fully deterministic scenario pool."""
    pool: list[ModelPersonaClassifyRequest] = []

    # 1. Empty signals, no existing profile -> insufficient_data.
    pool.append(
        ModelPersonaClassifyRequest(user_id="user-1", signals=[], existing_profile=None)
    )

    # 2. Empty signals, existing profile -> returns it unchanged (signals_processed=0).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[],
            existing_profile=_profile(
                technical_level=EnumTechnicalLevel.ADVANCED,
                vocabulary_complexity=0.8,
                preferred_tone=EnumPreferredTone.CONCISE,
                session_count=5,
                persona_version=5,
            ),
        )
    )

    # 3. Fresh user, one high-confidence technical_level signal -> new ADVANCED persona.
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(3, "technical_level", "advanced", 0.9)],
        )
    )

    # 4. Domain familiarity accumulation (2 * 0.1 = 0.2).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _sig(41, "domain_familiarity", "omnimarket"),
                _sig(42, "domain_familiarity", "omnimarket"),
            ],
        )
    )

    # 5. Domain familiarity cap at 1.0 (15 increments).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(100 + i, "domain_familiarity", "repo-x") for i in range(15)],
        )
    )

    # 6. Vocabulary EMA with existing profile (0.2*1.0 + 0.8*0.5 = 0.6).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(6, "vocabulary", "1.0", 0.8)],
            existing_profile=_profile(
                technical_level=EnumTechnicalLevel.INTERMEDIATE,
                vocabulary_complexity=0.5,
                preferred_tone=EnumPreferredTone.EXPLANATORY,
                session_count=5,
                persona_version=5,
            ),
        )
    )

    # 7. Vocabulary with a non-numeric value -> ValueError branch (skipped).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(7, "vocabulary", "not-a-number", 0.8)],
        )
    )

    # 8. Technical level shifts on an early session (session_count < 3).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(8, "technical_level", "advanced", 0.9)],
            existing_profile=_profile(
                technical_level=EnumTechnicalLevel.BEGINNER,
                vocabulary_complexity=0.3,
                preferred_tone=EnumPreferredTone.EXPLANATORY,
                session_count=2,
                persona_version=2,
            ),
        )
    )

    # 9. Conservatism: proposed == existing (ADVANCED) at session_count >= 3 -> no shift.
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _sig(90, "technical_level", "advanced", 0.9),
                _sig(91, "technical_level", "advanced", 0.9),
                _sig(92, "technical_level", "advanced", 0.9),
                _sig(93, "technical_level", "beginner", 0.8),
                _sig(94, "technical_level", "beginner", 0.8),
            ],
            existing_profile=_profile(
                technical_level=EnumTechnicalLevel.ADVANCED,
                vocabulary_complexity=0.8,
                preferred_tone=EnumPreferredTone.CONCISE,
                session_count=5,
                persona_version=5,
            ),
        )
    )

    # 10. Conservatism BLOCK: proposed (beginner) != existing (INTERMEDIATE) but the
    #     proposed vote share is below 60% -> level held at INTERMEDIATE.
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _sig(110, "technical_level", "beginner", 0.9),
                _sig(111, "technical_level", "beginner", 0.9),
                _sig(112, "technical_level", "advanced", 0.9),
                _sig(113, "technical_level", "advanced", 0.9),
                _sig(114, "technical_level", "intermediate", 0.9),
            ],
            existing_profile=_profile(
                technical_level=EnumTechnicalLevel.INTERMEDIATE,
                vocabulary_complexity=0.5,
                preferred_tone=EnumPreferredTone.EXPLANATORY,
                session_count=5,
                persona_version=5,
            ),
        )
    )

    # 11. Preferred-tone signals with ordered timestamps -> tone mode (non-default).
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _sig(120, "preferred_tone", "concise", second=1),
                _sig(121, "preferred_tone", "concise", second=2),
                _sig(122, "preferred_tone", "explanatory", second=3),
            ],
        )
    )

    # 12. Low-confidence technical_level signal (< 0.7) is filtered out entirely.
    pool.append(
        ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_sig(12, "technical_level", "advanced", 0.5)],
        )
    )

    return pool


def normalize_output(output_json: dict[str, Any]) -> dict[str, Any]:
    """Mask the volatile ``persona.created_at`` so goldens are byte-stable."""
    persona = output_json.get("persona")
    if isinstance(persona, dict) and "created_at" in persona:
        persona = {**persona, "created_at": CREATED_AT_SENTINEL}
        return {**output_json, "persona": persona}
    return output_json


VOLATILE_MASK = ["persona.created_at"]
