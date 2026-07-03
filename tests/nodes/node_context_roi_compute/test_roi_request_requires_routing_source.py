# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD test 2 (OMN-12845 / M5): ModelContextRoiRequest exposes routing_source.

The BAC plan hard-requires per-row metadata
``context_pack_hash, factor_subset_hash, model, provider, endpoint, routing_source``
so a "winning factor" can never be a routing-authority artifact. The ROI request
row model already carries the first five; this asserts the missing sixth field is
present on every captured runner row.
"""

from __future__ import annotations

from omnimarket.nodes.node_context_roi_compute.models.model_context_roi_request import (
    ModelArmRunRow,
    ModelContextRoiRequest,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)


def _row(**overrides: object) -> ModelArmRunRow:
    base: dict[str, object] = {
        "task_id": "task-1",
        "arm_label": EnumArmLabel.OFF,
        "trial_index": 0,
        "run_id": "run-1",
        "first_pass_success": True,
        "final_success": True,
        "attempt_count": 1,
    }
    base.update(overrides)
    return ModelArmRunRow(**base)  # type: ignore[arg-type]


class TestRoiRequestRequiresRoutingSource:
    def test_arm_run_row_exposes_routing_source(self) -> None:
        """The captured runner row carries routing_source as a typed field."""
        assert "routing_source" in ModelArmRunRow.model_fields

    def test_routing_source_round_trips(self) -> None:
        row = _row(routing_source="routing_tier:local-coder")
        assert row.routing_source == "routing_tier:local-coder"

    def test_routing_source_defaults_none_when_absent(self) -> None:
        """Absent routing_source is an explicit None, never a silent fabricated value."""
        row = _row()
        assert row.routing_source is None

    def test_full_required_metadata_set_present(self) -> None:
        """All six plan-required per-row metadata fields exist on the row model."""
        required = {
            "context_pack_hash",
            "factor_subset_hash",
            "model_id",
            "provider",
            "endpoint_ref",
            "routing_source",
        }
        assert required.issubset(set(ModelArmRunRow.model_fields))

    def test_request_accepts_rows_with_routing_source(self) -> None:
        request = ModelContextRoiRequest(
            run_id="run-1",
            manifest_id="manifest-1",
            rows=(_row(routing_source="routing_tier:local-coder"),),
        )
        assert request.rows[0].routing_source == "routing_tier:local-coder"
