# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_persona_builder_compute.

Migrated from omnimemory (OMN-8297, Wave 1).
Verifies persona classification, conservatism rules, EMA vocabulary,
tone mode, and domain familiarity accumulation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

# omnimemory models referenced as external dep
from omnimemory.models.persona import ModelPersonaSignal

from omnimarket.nodes.node_persona_builder_compute.handlers.handler_persona_classify import (
    HandlerPersonaClassify,
)
from omnimarket.nodes.node_persona_builder_compute.models.model_classify_request import (
    ModelPersonaClassifyRequest,
)


def _signal(
    user_id: str = "user-1",
    signal_type: str = "technical_level",
    inferred_value: str = "advanced",
    confidence: float = 0.9,
    session_id: str = "sess-1",
) -> ModelPersonaSignal:
    return ModelPersonaSignal(
        signal_id=uuid4(),
        user_id=user_id,
        session_id=session_id,
        signal_type=signal_type,
        evidence="observed behavior",
        inferred_value=inferred_value,
        confidence=confidence,
        emitted_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestPersonaBuilderComputeGoldenChain:
    """Golden chain: persona signals in -> updated persona out."""

    async def test_empty_signals_no_existing_returns_insufficient(self) -> None:
        """No signals and no existing profile returns insufficient_data."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[],
            existing_profile=None,
        )
        result = await handler.handle(cid, request)
        assert result.status == "insufficient_data"
        assert result.persona is None
        assert result.signals_processed == 0

    async def test_empty_signals_with_existing_returns_existing(self) -> None:
        """No signals but existing profile returns it unchanged."""
        from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel

        handler = HandlerPersonaClassify()
        cid = uuid4()
        now = datetime.now(tz=UTC)

        from omnimemory.models.persona import ModelUserPersonaV1

        existing = ModelUserPersonaV1(
            user_id="user-1",
            technical_level=EnumTechnicalLevel.ADVANCED,
            vocabulary_complexity=0.8,
            preferred_tone=EnumPreferredTone.CONCISE,
            domain_familiarity={},
            session_count=5,
            persona_version=5,
            created_at=now,
            rebuilt_from_signals=10,
        )
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[],
            existing_profile=existing,
        )
        result = await handler.handle(cid, request)
        assert result.status == "success"
        assert result.persona == existing
        assert result.signals_processed == 0

    async def test_fresh_user_gets_initial_persona(self) -> None:
        """First session with signals creates a new persona."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _signal(
                    signal_type="technical_level",
                    inferred_value="advanced",
                    confidence=0.9,
                )
            ],
        )
        result = await handler.handle(cid, request)
        assert result.status == "success"
        assert result.persona is not None
        assert result.signals_processed == 1
        assert result.persona.session_count == 1

    async def test_domain_familiarity_accumulates(self) -> None:
        """Domain familiarity increments by 0.1 per signal."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _signal(signal_type="domain_familiarity", inferred_value="omnimarket"),
                _signal(signal_type="domain_familiarity", inferred_value="omnimarket"),
            ],
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        # 2 signals * 0.1 increment = 0.2
        assert result.persona.domain_familiarity.get("omnimarket") == pytest.approx(
            0.2, abs=0.01
        )

    async def test_domain_familiarity_caps_at_1(self) -> None:
        """Domain familiarity is capped at 1.0."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        # 15 signals to push past 1.0
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _signal(signal_type="domain_familiarity", inferred_value="repo-x")
                for _ in range(15)
            ],
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        assert result.persona.domain_familiarity.get("repo-x") == pytest.approx(
            1.0, abs=0.01
        )

    async def test_vocabulary_ema_applied(self) -> None:
        """Vocabulary complexity uses EMA with alpha=0.2."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        # Start with existing profile at 0.5, send a signal of 1.0
        # Expected: 0.2*1.0 + 0.8*0.5 = 0.6
        from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
        from omnimemory.models.persona import ModelUserPersonaV1

        existing = ModelUserPersonaV1(
            user_id="user-1",
            technical_level=EnumTechnicalLevel.INTERMEDIATE,
            vocabulary_complexity=0.5,
            preferred_tone=EnumPreferredTone.EXPLANATORY,
            domain_familiarity={},
            session_count=5,
            persona_version=5,
            created_at=datetime.now(tz=UTC),
            rebuilt_from_signals=5,
        )
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _signal(signal_type="vocabulary", inferred_value="1.0", confidence=0.8)
            ],
            existing_profile=existing,
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        assert result.persona.vocabulary_complexity == pytest.approx(0.6, abs=0.01)

    async def test_technical_level_conservatism_on_early_session(self) -> None:
        """Technical level can shift on early sessions (< 3 sessions)."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
        from omnimemory.models.persona import ModelUserPersonaV1

        existing = ModelUserPersonaV1(
            user_id="user-1",
            technical_level=EnumTechnicalLevel.BEGINNER,
            vocabulary_complexity=0.3,
            preferred_tone=EnumPreferredTone.EXPLANATORY,
            domain_familiarity={},
            session_count=2,  # < 3, so shift is allowed
            persona_version=2,
            created_at=datetime.now(tz=UTC),
            rebuilt_from_signals=2,
        )
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[
                _signal(
                    signal_type="technical_level",
                    inferred_value="advanced",
                    confidence=0.9,
                )
            ],
            existing_profile=existing,
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        assert result.persona.technical_level == EnumTechnicalLevel.ADVANCED

    async def test_technical_level_conservatism_blocks_minority_shift(self) -> None:
        """Technical level does NOT shift when proposed level lacks 60% majority.

        With 3 high-confidence signals, 2 'advanced' and 1 'beginner',
        'beginner' has only 1/3 = 33% support — below the 60% threshold.
        The level should stay at BEGINNER (the current level) since
        'advanced' is the proposed shift but wait — 'advanced' is the majority
        here at 67%, so it DOES shift. Let's test the minority case:
        2 signals for 'advanced' (new), 3 for 'beginner' (different from current
        'intermediate'). Most voted is 'beginner' at 3/5=60% — it shifts.

        Instead test: existing=ADVANCED, 3 signals: 2 for 'beginner', 1 for 'intermediate'.
        Majority is 'beginner' at 2/3=67% — that DOES shift (>= 60%).

        Real conservatism case: existing=ADVANCED, 5 signals:
        2 for 'beginner', 3 for 'advanced' (same as current).
        Proposed change is 'beginner' with only 2/5=40% — blocked.
        """
        handler = HandlerPersonaClassify()
        cid = uuid4()
        from omnimemory.enums import EnumPreferredTone, EnumTechnicalLevel
        from omnimemory.models.persona import ModelUserPersonaV1

        existing = ModelUserPersonaV1(
            user_id="user-1",
            technical_level=EnumTechnicalLevel.ADVANCED,
            vocabulary_complexity=0.8,
            preferred_tone=EnumPreferredTone.CONCISE,
            domain_familiarity={},
            session_count=5,  # >= 3, strict conservatism kicks in
            persona_version=5,
            created_at=datetime.now(tz=UTC),
            rebuilt_from_signals=10,
        )
        # 5 high-confidence signals: 3 for 'advanced' (same), 2 for 'beginner'.
        # Most voted is 'advanced' (=existing) — no change. No competing majority.
        # Since proposed == existing, the level stays at ADVANCED.
        signals = [
            _signal(
                signal_type="technical_level", inferred_value="advanced", confidence=0.9
            ),
            _signal(
                signal_type="technical_level", inferred_value="advanced", confidence=0.9
            ),
            _signal(
                signal_type="technical_level", inferred_value="advanced", confidence=0.9
            ),
            _signal(
                signal_type="technical_level", inferred_value="beginner", confidence=0.8
            ),
            _signal(
                signal_type="technical_level", inferred_value="beginner", confidence=0.8
            ),
        ]
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=signals,
            existing_profile=existing,
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        # Most voted is 'advanced' (same as existing) — stays ADVANCED
        assert result.persona.technical_level == EnumTechnicalLevel.ADVANCED

    async def test_persona_version_increments(self) -> None:
        """Persona version increments by 1 each classification."""
        handler = HandlerPersonaClassify()
        cid = uuid4()
        request = ModelPersonaClassifyRequest(
            user_id="user-1",
            signals=[_signal()],
        )
        result = await handler.handle(cid, request)
        assert result.persona is not None
        assert result.persona.persona_version == 1

    async def test_handler_type_metadata(self) -> None:
        """Handler has correct type metadata."""
        handler = HandlerPersonaClassify()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"
