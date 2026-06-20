# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 4 (OMN-12845 / M5): observational rows cannot claim effectiveness.

Attribution-honesty rule (BAC plan theme-5, lines 119-121): only a
controlled-intervention runtime row (randomized arm order, fixed model/temp) may
write an effectiveness CLAIM onto a stored capsule. Any observational /
non-controlled row may only generate a HYPOTHESIS — it is rejected from the
effectiveness-claim path and never folded onto a capsule as a measured score.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.events.capsule_feedback import (
    EnumRowAttributionClass,
    ModelScoredRuntimeRow,
)
from omnimarket.nodes.node_capsule_effectiveness_feedback_reducer.handlers.handler_capsule_effectiveness_feedback import (
    AttributionHonestyError,
    HandlerCapsuleEffectivenessFeedback,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_EVENT_TS = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _observational_row() -> ModelScoredRuntimeRow:
    return ModelScoredRuntimeRow(
        factor=EnumContextFactor.EXEMPLAR,
        content="observed body",
        source_artifact="exemplars/obs.py",
        source_commit="obs123",
        validity_scope="repo:omnimarket",
        final_success_rate=0.9,
        first_pass_rate=0.8,
        cost_per_success_usd=0.30,
        proof_class=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        attribution_class=EnumRowAttributionClass.OBSERVATIONAL,
        routing_source="routing_tier:local-coder",
        event_timestamp=_EVENT_TS,
    )


class TestObservationalRowCannotClaimEffectiveness:
    def test_observational_row_does_not_write_capsule(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerCapsuleEffectivenessFeedback()

        result = handler.project(_observational_row(), db)

        assert result.effectiveness_claim_written is False
        assert result.hypothesis_recorded is True
        # No effectiveness claim landed on any capsule row.
        assert db.query("capsule_store") == []

    def test_strict_mode_rejects_observational_from_claim_path(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerCapsuleEffectivenessFeedback()

        with pytest.raises(AttributionHonestyError):
            handler.write_effectiveness_claim(_observational_row(), db)

    def test_controlled_row_is_accepted_by_claim_path(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerCapsuleEffectivenessFeedback()

        controlled = _observational_row().model_copy(
            update={
                "attribution_class": EnumRowAttributionClass.CONTROLLED_INTERVENTION
            }
        )
        applied = handler.write_effectiveness_claim(controlled, db)
        assert applied.effectiveness_claim_written is True
        assert db.query("capsule_store", {"capsule_hash": applied.capsule_hash})
