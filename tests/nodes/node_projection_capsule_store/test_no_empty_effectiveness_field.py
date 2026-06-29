# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 3 (OMN-12842): no empty effectiveness fields on scored rows.

A capsule row that has been scored at least once with a NULL effectiveness
field is a hard violation. This is encoded in the model validator (and mirrored
by a DB CHECK constraint in the migration), NOT in a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import ValidationError

from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_identity import (
    EnumCapsuleSchemaVersion,
    ModelCapsuleIdentity,
)
from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_record import (
    ModelCapsuleEffectiveness,
    ModelCapsuleRecord,
)

_EVENT_TS = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _identity() -> ModelCapsuleIdentity:
    return ModelCapsuleIdentity.from_provenance(
        factor=EnumContextFactor.EXEMPLAR,
        content="exemplar body",
        source_artifact="exemplars/foo.py",
        source_commit="abc123",
        schema_version=EnumCapsuleSchemaVersion.V1,
    )


def _effectiveness() -> ModelCapsuleEffectiveness:
    return ModelCapsuleEffectiveness(
        success_rate=0.8,
        first_pass_rate=0.6,
        cost_per_success=0.42,
        hit_count=1,
        last_scored=_EVENT_TS,
    )


class TestNoEmptyEffectivenessField:
    def test_scored_record_with_null_success_rate_raises(self) -> None:
        with pytest.raises(ValidationError):
            ModelCapsuleEffectiveness(
                success_rate=None,  # type: ignore[arg-type]
                first_pass_rate=0.6,
                cost_per_success=0.42,
                hit_count=1,
                last_scored=_EVENT_TS,
            )

    def test_scored_record_with_null_first_pass_rate_raises(self) -> None:
        with pytest.raises(ValidationError):
            ModelCapsuleEffectiveness(
                success_rate=0.8,
                first_pass_rate=None,  # type: ignore[arg-type]
                cost_per_success=0.42,
                hit_count=1,
                last_scored=_EVENT_TS,
            )

    def test_scored_record_with_zero_hit_count_raises(self) -> None:
        """A scored row must have hit_count >= 1 (it has been scored)."""
        with pytest.raises(ValidationError):
            ModelCapsuleEffectiveness(
                success_rate=0.8,
                first_pass_rate=0.6,
                cost_per_success=0.42,
                hit_count=0,
                last_scored=_EVENT_TS,
            )

    def test_well_formed_scored_record_is_valid(self) -> None:
        record = ModelCapsuleRecord(
            identity=_identity(),
            effectiveness=_effectiveness(),
            validity_scope="repo:omnimarket",
        )
        assert record.effectiveness.success_rate == 0.8
        assert record.effectiveness.hit_count == 1
