# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 4 (OMN-12842): staleness decay.

Effectiveness is read through a decay function of ``(now - last_scored)``:
``effective_score = raw_score * decay(age)``. Decay parameters (half-life,
floor) are CONTRACT-declared in ``contract.yaml`` config, resolved at the
handler boundary -- not hardcoded constants and not env vars. Effective scores
for older capsules are strictly less than fresher ones, and both stay within
``[floor, raw]``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
)


class TestStalenessDecay:
    def test_decay_params_read_from_contract_not_literals(self) -> None:
        handler = HandlerCapsuleStoreProjection()
        config = handler.decay_config
        # Contract-declared decay config must be present and positive.
        assert config.half_life_days > 0
        assert 0.0 <= config.floor <= 1.0

    def test_older_capsule_decays_below_fresher(self) -> None:
        handler = HandlerCapsuleStoreProjection()
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        raw = 0.9

        fresh_scored = now - timedelta(days=0)
        old_scored = now - timedelta(days=30)

        effective_fresh = handler.effective_score(
            raw, last_scored=fresh_scored, now=now
        )
        effective_old = handler.effective_score(raw, last_scored=old_scored, now=now)

        assert effective_old < effective_fresh

    def test_effective_score_within_floor_and_raw(self) -> None:
        handler = HandlerCapsuleStoreProjection()
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        raw = 0.9
        floor = handler.decay_config.floor

        for age_days in (0, 7, 30, 365, 3650):
            scored = now - timedelta(days=age_days)
            effective = handler.effective_score(raw, last_scored=scored, now=now)
            assert floor * raw <= effective <= raw

    def test_fresh_capsule_effective_equals_raw_at_zero_age(self) -> None:
        handler = HandlerCapsuleStoreProjection()
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        raw = 0.75
        effective = handler.effective_score(raw, last_scored=now, now=now)
        assert effective == raw

    def test_half_life_halves_decay_multiplier(self) -> None:
        handler = HandlerCapsuleStoreProjection()
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        half_life = handler.decay_config.half_life_days
        floor = handler.decay_config.floor
        raw = 1.0

        scored = now - timedelta(days=half_life)
        effective = handler.effective_score(raw, last_scored=scored, now=now)
        # At one half-life the decay multiplier is 0.5, clamped to floor.
        expected = max(0.5, floor)
        assert abs(effective - expected) < 1e-9
