# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persona classification handler — pure compute, no I/O.

Migrated from omnimemory (OMN-8297, Wave 1).

Takes a batch of PersonaSignal events and an optional existing PersonaProfile,
produces an updated PersonaProfile using conservative update rules.

CONSERVATISM RULES (prevent thrashing on weak evidence):
- technical_level: requires 3+ sessions with consistent high-confidence (>0.7)
  signals before changing level.
- vocabulary_complexity: exponential moving average with alpha=0.2 (slow drift)
- preferred_tone: mode of last 10 signals.
- domain_familiarity: increment per-repo familiarity by 0.1 per session. Capped at 1.0.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
from omnimemory.models.persona import ModelPersonaSignal, ModelUserPersonaV1

from omnimarket.nodes.node_persona_builder_compute.models.model_classify_request import (
    ModelPersonaClassifyRequest,
)
from omnimarket.nodes.node_persona_builder_compute.models.model_classify_result import (
    ModelPersonaClassifyResult,
)

__all__ = ["HandlerPersonaClassify"]

# Confidence threshold for signals to influence technical_level
_TECH_LEVEL_CONFIDENCE_THRESHOLD = 0.7
# Minimum sessions before technical_level can shift
_TECH_LEVEL_MIN_SESSIONS = 3
# EMA alpha for vocabulary_complexity
_VOCAB_EMA_ALPHA = 0.2
# Max signals to consider for tone mode
_TONE_WINDOW_SIZE = 10
# Per-session domain familiarity increment
_DOMAIN_INCREMENT = 0.1
# Domain familiarity cap
_DOMAIN_CAP = 1.0


class HandlerPersonaClassify:
    """Pure compute handler for persona classification.

    Wraps the classify_persona pure function in an omnimarket handler interface.
    All logic is stateless — no I/O, no side effects.
    """

    handler_type = "NODE_HANDLER"
    handler_category = "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        input_data: ModelPersonaClassifyRequest,
    ) -> ModelPersonaClassifyResult:
        """Classify persona signals into an updated profile.

        Args:
            correlation_id: Tracing ID (unused in pure compute, present for interface consistency).
            input_data: Signals batch and optional existing profile.

        Returns:
            Updated persona snapshot.
        """
        return _classify_persona(input_data)


def _classify_persona(
    request: ModelPersonaClassifyRequest,
) -> ModelPersonaClassifyResult:
    """Incrementally update persona profile from new signals. Pure function."""
    signals = request.signals
    existing = request.existing_profile

    if not signals:
        if existing is not None:
            return ModelPersonaClassifyResult(
                status="success",
                persona=existing,
                signals_processed=0,
            )
        return ModelPersonaClassifyResult(
            status="insufficient_data",
            signals_processed=0,
        )

    technical_level = _classify_technical_level(signals, existing)
    vocabulary = _classify_vocabulary(signals, existing)
    tone = _classify_tone(signals)
    domains = _classify_domains(signals, existing)

    session_count = (existing.session_count if existing else 0) + 1
    persona_version = (existing.persona_version if existing else 0) + 1
    now = datetime.now(tz=UTC)

    persona = ModelUserPersonaV1(
        user_id=request.user_id,
        agent_id=existing.agent_id if existing else None,
        technical_level=technical_level,
        vocabulary_complexity=vocabulary,
        preferred_tone=tone,
        domain_familiarity=domains,
        session_count=session_count,
        persona_version=persona_version,
        created_at=now,
        rebuilt_from_signals=len(signals),
    )

    return ModelPersonaClassifyResult(
        status="success",
        persona=persona,
        signals_processed=len(signals),
    )


def _classify_technical_level(
    signals: list[ModelPersonaSignal],
    existing: ModelUserPersonaV1 | None,
) -> EnumTechnicalLevel:
    tech_signals = [
        s
        for s in signals
        if s.signal_type == "technical_level"
        and s.confidence >= _TECH_LEVEL_CONFIDENCE_THRESHOLD
    ]

    if not tech_signals:
        return existing.technical_level if existing else EnumTechnicalLevel.INTERMEDIATE

    votes: Counter[str] = Counter()
    for s in tech_signals:
        votes[s.inferred_value] += 1

    proposed_level_str = votes.most_common(1)[0][0]
    level_map = {member.value: member for member in EnumTechnicalLevel}
    proposed_level = level_map.get(proposed_level_str, EnumTechnicalLevel.INTERMEDIATE)

    if existing is None:
        return proposed_level

    if existing.session_count < _TECH_LEVEL_MIN_SESSIONS:
        return proposed_level

    if proposed_level != existing.technical_level:
        total_high_conf = len(tech_signals)
        proposed_count = votes[proposed_level_str]
        if proposed_count >= total_high_conf * 0.6:
            return proposed_level
        return existing.technical_level

    return proposed_level


def _classify_vocabulary(
    signals: list[ModelPersonaSignal],
    existing: ModelUserPersonaV1 | None,
) -> float:
    vocab_signals = [s for s in signals if s.signal_type == "vocabulary"]
    if not vocab_signals:
        return existing.vocabulary_complexity if existing else 0.5

    current = existing.vocabulary_complexity if existing else 0.5
    for s in vocab_signals:
        try:
            new_value = max(0.0, min(1.0, float(s.inferred_value)))
            current = (_VOCAB_EMA_ALPHA * new_value) + (
                (1 - _VOCAB_EMA_ALPHA) * current
            )
        except ValueError:
            continue

    return round(current, 3)


def _classify_tone(signals: list[ModelPersonaSignal]) -> EnumPreferredTone:
    tone_signals = [s for s in signals if s.signal_type == "preferred_tone"]
    if not tone_signals:
        return EnumPreferredTone.EXPLANATORY

    recent = sorted(tone_signals, key=lambda s: s.emitted_at, reverse=True)[
        :_TONE_WINDOW_SIZE
    ]
    votes: Counter[str] = Counter()
    for s in recent:
        votes[s.inferred_value] += 1

    tone_str = votes.most_common(1)[0][0]
    tone_map = {member.value: member for member in EnumPreferredTone}
    return tone_map.get(tone_str, EnumPreferredTone.EXPLANATORY)


def _classify_domains(
    signals: list[ModelPersonaSignal],
    existing: ModelUserPersonaV1 | None,
) -> dict[str, float]:
    domains = dict(existing.domain_familiarity) if existing else {}
    for s in signals:
        if s.signal_type == "domain_familiarity":
            current = domains.get(s.inferred_value, 0.0)
            domains[s.inferred_value] = min(current + _DOMAIN_INCREMENT, _DOMAIN_CAP)
    return domains
