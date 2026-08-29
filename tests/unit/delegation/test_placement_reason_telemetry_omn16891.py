# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every routing decision records WHY this backend was chosen [OMN-16891].

``ModelDelegatedFixAttemptRecord`` already carries ``tier``, ``backend_id``,
``cost_usd``, ``task_type``, ``outcome`` and ``accepted`` — i.e. *what* ran and
*how it went*, but never *why it was placed there*. Without the why, the
placement table can only be re-argued, not tuned: a 60% acceptance rate on the
free coder rung means something completely different when the rung was chosen
by class-affinity than when it was chosen because local was saturated.

``placement_reason`` closes that gap so the OMN-13940 widening bar (>=70% over
>=20 samples) can be computed per PLACEMENT CAUSE rather than smeared across
all of them.

Sink choice (deliberate, see the dead-telemetry finding on OMN-16891): the
Postgres routing tables on the dev lane — ``routing_outcomes``,
``infra_routing_decisions``, ``llm_routing_decisions``,
``agent_routing_decisions`` — are ALL EMPTY all-time (verified 2026-08-28
against ``omnibase-infra-postgres``). Wiring ``placement_reason`` into them
would write to a path nothing reads. The JSONL acceptance sink IS live: it is
constructed and written on the real delegated-fix path
(``handler_delegated_fix``), so the field goes there.

The model is ``frozen=True, extra="forbid"``, so the field carries a default —
rows written before this change must still read back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_acceptance_telemetry import (
    EnumPlacementReason,
    JsonlAcceptanceTelemetryRecorder,
    ModelDelegatedFixAttemptRecord,
)


def _record(**overrides: object) -> ModelDelegatedFixAttemptRecord:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 2192,
        "block_reason": "ci_red",
        "task_type": "code_generation",
        "delegation_model": "qwen3.8",
        "backend_id": "local-coder",
        "tier": "local",
        "outcome": "accepted",
        "accepted": True,
        "recorded_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ModelDelegatedFixAttemptRecord(**base)  # type: ignore[arg-type]


@pytest.mark.unit
class TestPlacementReasonField:
    def test_every_declared_reason_is_accepted(self) -> None:
        """The four causes a placement can have, per the OMN-16891 directive."""
        for reason in (
            EnumPlacementReason.LOCAL_FIRST,
            EnumPlacementReason.SATURATION_ESCALATE,
            EnumPlacementReason.CLASS_AFFINITY,
            EnumPlacementReason.FALLBACK,
        ):
            assert _record(placement_reason=reason).placement_reason is reason

    def test_field_defaults_so_older_rows_still_read_back(self) -> None:
        """``extra="forbid"`` + no default would break readback of Slice 1 rows."""
        assert _record().placement_reason is None

    def test_reason_survives_a_jsonl_round_trip(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The value must reach the sink, not just the model.

        A field that serialises but is dropped on read makes the bar
        uncomputable, which is the whole point of adding it.
        """
        recorder = JsonlAcceptanceTelemetryRecorder(state_dir=tmp_path)
        recorder.record(
            _record(
                tier="cheap_frontier",
                backend_id="openrouter-qwen3-coder-480b",
                placement_reason=EnumPlacementReason.SATURATION_ESCALATE,
            )
        )
        samples = recorder.read_samples()
        assert len(samples) == 1
        assert samples[0].placement_reason is EnumPlacementReason.SATURATION_ESCALATE
        assert samples[0].tier == "cheap_frontier"

    def test_rows_are_filterable_by_placement_reason(self) -> None:
        """Per-cause acceptance is the tuning signal the field exists for."""
        rows = [
            _record(placement_reason=EnumPlacementReason.LOCAL_FIRST, accepted=True),
            _record(
                placement_reason=EnumPlacementReason.SATURATION_ESCALATE, accepted=False
            ),
            _record(
                placement_reason=EnumPlacementReason.SATURATION_ESCALATE, accepted=True
            ),
        ]
        escalated = [
            r
            for r in rows
            if r.placement_reason is EnumPlacementReason.SATURATION_ESCALATE
        ]
        assert len(escalated) == 2
        assert sum(r.accepted for r in escalated) / len(escalated) == 0.5

    def test_unknown_reason_is_rejected(self) -> None:
        """A free-text reason would make the field unaggregatable."""
        with pytest.raises(ValidationError, match="placement_reason"):
            _record(placement_reason="because-i-said-so")
