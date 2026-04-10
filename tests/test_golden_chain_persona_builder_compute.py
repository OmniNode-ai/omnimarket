# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_persona_builder_compute.

Verifies persona classification from signals, conservatism rules,
empty signal handling, idempotency, and incremental updates.

Migrated from omnimemory to omnimarket (OMN-8297).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
from omnimemory.models.persona import ModelPersonaSignal

from omnimarket.nodes.node_persona_builder_compute.handlers.handler_persona_classify import (
    HandlerPersonaClassify,
    classify_persona,
)
from omnimarket.nodes.node_persona_builder_compute.models import (
    ModelPersonaClassifyRequest,
    ModelPersonaClassifyResult,
)


def _signal(
    signal_type: str,
    inferred_value: str,
    confidence: float = 0.9,
    user_id: str = "user-test",
    session_id: str = "session-test",
) -> ModelPersonaSignal:
    return ModelPersonaSignal(
        user_id=user_id,
        session_id=session_id,
        signal_type=signal_type,
        inferred_value=inferred_value,
        confidence=confidence,
        evidence="test-evidence",
        emitted_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestPersonaBuilderComputeGoldenChain:
    """Golden chain: persona signals in -> updated PersonaProfile out."""

    def test_empty_signals_no_existing_returns_insufficient_data(self) -> None:
        """No signals and no existing profile returns insufficient_data."""
        request = ModelPersonaClassifyRequest(
            user_id="user-001",
            signals=[],
        )
        result = classify_persona(request)
        assert result.status == "insufficient_data"
        assert result.signals_processed == 0
        assert result.persona is None

    def test_empty_signals_with_existing_returns_success(self) -> None:
        """No signals with existing profile returns success with same profile."""
        request_initial = ModelPersonaClassifyRequest(
            user_id="user-001",
            signals=[_signal("technical_level", "advanced", confidence=0.95)],
        )
        initial_result = classify_persona(request_initial)
        assert initial_result.status == "success"
        assert initial_result.persona is not None

        request_empty = ModelPersonaClassifyRequest(
            user_id="user-001",
            signals=[],
            existing_profile=initial_result.persona,
        )
        result = classify_persona(request_empty)
        assert result.status == "success"
        assert result.signals_processed == 0
        assert result.persona == initial_result.persona

    def test_technical_level_signal_classification(self) -> None:
        """Technical level signal is classified correctly."""
        request = ModelPersonaClassifyRequest(
            user_id="user-001",
            signals=[_signal("technical_level", "advanced", confidence=0.9)],
        )
        result = classify_persona(request)
        assert result.status == "success"
        assert result.persona is not None
        assert result.persona.technical_level == EnumTechnicalLevel.ADVANCED
        assert result.signals_processed == 1

    def test_tone_signal_classification(self) -> None:
        """Preferred tone signal is classified correctly."""
        request = ModelPersonaClassifyRequest(
            user_id="user-002",
            signals=[_signal("preferred_tone", "concise", confidence=0.85)],
        )
        result = classify_persona(request)
        assert result.status == "success"
        assert result.persona is not None
        assert result.persona.preferred_tone == EnumPreferredTone.CONCISE

    def test_vocabulary_complexity_ema(self) -> None:
        """Vocabulary complexity uses EMA, bounded [0, 1]."""
        request = ModelPersonaClassifyRequest(
            user_id="user-003",
            signals=[_signal("vocabulary", "0.8", confidence=0.9)],
        )
        result = classify_persona(request)
        assert result.status == "success"
        assert result.persona is not None
        assert 0.0 <= result.persona.vocabulary_complexity <= 1.0

    def test_domain_familiarity_increments(self) -> None:
        """Domain familiarity increments per session, capped at 1.0."""
        request = ModelPersonaClassifyRequest(
            user_id="user-004",
            signals=[_signal("domain_familiarity", "python", confidence=0.9)],
        )
        result = classify_persona(request)
        assert result.status == "success"
        assert result.persona is not None
        assert result.persona.domain_familiarity.get("python", 0.0) == pytest.approx(
            0.1, abs=1e-9
        )

    def test_session_count_increments(self) -> None:
        """Session count increments on each classify call."""
        request = ModelPersonaClassifyRequest(
            user_id="user-005",
            signals=[_signal("technical_level", "intermediate", confidence=0.9)],
        )
        result1 = classify_persona(request)
        assert result1.persona is not None
        assert result1.persona.session_count == 1

        request2 = ModelPersonaClassifyRequest(
            user_id="user-005",
            signals=[_signal("technical_level", "intermediate", confidence=0.9)],
            existing_profile=result1.persona,
        )
        result2 = classify_persona(request2)
        assert result2.persona is not None
        assert result2.persona.session_count == 2

    def test_idempotency_same_signals(self) -> None:
        """Same signals on fresh profile produce identical technical_level."""
        signals = [_signal("technical_level", "advanced", confidence=0.9)]
        r1 = classify_persona(ModelPersonaClassifyRequest(user_id="u", signals=signals))
        r2 = classify_persona(ModelPersonaClassifyRequest(user_id="u", signals=signals))
        assert r1.persona is not None
        assert r2.persona is not None
        assert r1.persona.technical_level == r2.persona.technical_level

    def test_handler_class_classify_method(self) -> None:
        """HandlerPersonaClassify.classify delegates to classify_persona."""
        request = ModelPersonaClassifyRequest(
            user_id="user-006",
            signals=[_signal("technical_level", "beginner", confidence=0.95)],
        )
        result = HandlerPersonaClassify.classify(request)
        assert isinstance(result, ModelPersonaClassifyResult)
        assert result.status == "success"
        assert result.persona is not None
        assert result.persona.technical_level == EnumTechnicalLevel.BEGINNER

    def test_contract_output_model_fields(self) -> None:
        """Output model has all required contract fields."""
        request = ModelPersonaClassifyRequest(
            user_id="user-007",
            signals=[_signal("vocabulary", "0.6")],
        )
        result = classify_persona(request)
        assert hasattr(result, "status")
        assert hasattr(result, "persona")
        assert hasattr(result, "signals_processed")
        assert hasattr(result, "error_message")
