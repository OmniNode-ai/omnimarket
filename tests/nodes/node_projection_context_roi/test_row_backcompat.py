# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Back-compat and contract tests for the code-eval trial fields (OMN-23628).

Pins the design the source change must satisfy: ``ModelAttemptReductionRow``
gains the code-eval trial fields with defaults so an older row still
validates; the migration adds one column per field with defaults matching the
model; the contract exposes every new column and declares the DLQ topic; and
the projection stores the new columns (enums as their string value, None as
None).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.context_roi import (
    EnumContextRoiRowKind,
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

HANDLER = HandlerProjectionContextRoi()
CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_context_roi/contract.yaml")
MIGRATION_PATH = (
    Path("src/omnimarket/nodes/node_projection_context_roi/migrations")
    / "004_add_code_eval_trial_columns.sql"
)
DLQ_TOPIC = "onex.dlq.omnimarket.projection-context-roi-malformed.v1"

LEGACY_FIELDS = (
    "run_id",
    "correlation_id",
    "task_id",
    "run_order",
    "context_factor_subset",
    "context_pack_hash",
    "factor_subset_hash",
    "attempt_count",
    "first_pass_success",
    "final_success",
    "failure_stage",
    "prompt_tokens",
    "completion_tokens",
    "estimated_cost",
    "model_id",
    "provider",
    "endpoint_ref",
    "routing_source",
    "proof_class",
)

NEW_FIELDS = (
    "check_verdict",
    "unscorable_source",
    "row_kind",
    "try_end",
    "suite_digest",
    "size_band",
    "sequence_id",
    "try_ordinal",
    "diff_hash",
    "max_turns",
    "try_timeout_s",
    "generation_s",
    "env_sync_s",
    "check_s",
)

# SQL default literal per NOT NULL DEFAULT column, keyed by field name.
SQL_DEFAULTS = {
    "row_kind": "'try'",
    "try_end": "''",
    "suite_digest": "''",
    "sequence_id": "''",
    "diff_hash": "''",
    "size_band": "'S'",
    "try_ordinal": "0",
    "max_turns": "0",
    "try_timeout_s": "0",
    "generation_s": "0",
    "env_sync_s": "0",
    "check_s": "0",
}


def _legacy_row_dict() -> dict[str, object]:
    """A row dict holding only the fields the row had before the change."""
    return {
        "run_id": "run-001",
        "correlation_id": "c-1",
        "task_id": "task-A",
        "run_order": 1,
        "context_factor_subset": "golden_exemplar",
        "context_pack_hash": "abc123",
        "factor_subset_hash": "sha256:real-factor-hash",
        "attempt_count": 1,
        "first_pass_success": True,
        "final_success": True,
        "failure_stage": "none",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "estimated_cost": 0.0,
        "model_id": "qwen3-coder-30b",
        "provider": "local",
        "endpoint_ref": "local-coder",
        "routing_source": "routing_tier:local-coder",
        "proof_class": "runtime-observed-only",
    }


def _contract() -> dict[str, object]:
    with open(CONTRACT_PATH) as handle:
        return dict(yaml.safe_load(handle))


def _migration_text() -> str:
    return MIGRATION_PATH.read_text()


class TestRowBackcompat:
    def test_legacy_row_dict_validates_and_new_fields_read_defaults(self) -> None:
        row = ModelAttemptReductionRow.model_validate(_legacy_row_dict())
        assert row.check_verdict is None
        assert row.unscorable_source is None
        assert row.row_kind is EnumContextRoiRowKind.TRY
        assert row.try_end == ""
        assert row.suite_digest == ""
        assert row.size_band is EnumSizeBand.S
        assert row.sequence_id == ""
        assert row.try_ordinal == 0
        assert row.diff_hash == ""
        assert row.max_turns == 0
        assert row.try_timeout_s == 0.0
        assert row.generation_s == 0.0
        assert row.env_sync_s == 0.0
        assert row.check_s == 0.0

    def test_legacy_run_result_payload_validates(self) -> None:
        result = ModelContextRoiRunResult.model_validate(
            {"run_id": "run-001", "rows": [_legacy_row_dict()]}
        )
        assert result.run_id == "run-001"
        assert len(result.rows) == 1
        assert result.rows[0].row_kind is EnumContextRoiRowKind.TRY

    def test_extra_forbid_still_refuses_an_unknown_key(self) -> None:
        payload = _legacy_row_dict()
        payload["not_a_real_field"] = "x"
        with pytest.raises(ValidationError):
            ModelAttemptReductionRow.model_validate(payload)

    def test_unscorable_source_with_pass_verdict_is_refused(self) -> None:
        payload = _legacy_row_dict()
        payload["check_verdict"] = "pass"
        payload["unscorable_source"] = "harness"
        with pytest.raises(ValidationError):
            ModelAttemptReductionRow.model_validate(payload)

    def test_unscorable_source_with_unscorable_verdict_is_accepted(self) -> None:
        payload = _legacy_row_dict()
        payload["check_verdict"] = "unscorable"
        payload["unscorable_source"] = "harness"
        row = ModelAttemptReductionRow.model_validate(payload)
        assert row.check_verdict is EnumCodeScoreVerdict.UNSCORABLE
        assert row.unscorable_source is EnumCodeScoreSource.HARNESS

    @pytest.mark.parametrize("field", NEW_FIELDS)
    def test_migration_adds_every_new_field_as_a_column(self, field: str) -> None:
        text = _migration_text()
        assert field in text

    def test_migration_contains_no_dangerous_words(self) -> None:
        text = _migration_text().upper()
        for word in ("DROP", "DELETE", "UPDATE", "TRUNCATE"):
            assert word not in text

    @pytest.mark.parametrize("field", [f for f in NEW_FIELDS if f in SQL_DEFAULTS])
    def test_migration_defaults_match_model_defaults(self, field: str) -> None:
        text = _migration_text()
        expected = SQL_DEFAULTS[field]
        assert f"DEFAULT {expected}" in text

    def test_contract_exposes_every_new_column(self) -> None:
        contract = _contract()
        exposures = contract["projection_api"]["exposures"]
        columns = exposures[0]["columns"]
        for field in NEW_FIELDS:
            assert field in columns

    def test_contract_declares_the_dlq_topic(self) -> None:
        contract = _contract()
        assert contract["event_bus"]["dlq_topics"] == [DLQ_TOPIC]

    def test_projecting_a_legacy_row_stores_migration_defaults(self) -> None:
        db = InmemoryDatabaseAdapter()
        row = ModelAttemptReductionRow.model_validate(_legacy_row_dict())
        result = ModelContextRoiRunResult(run_id="run-001", rows=(row,))
        HANDLER.project(result, db)
        stored = db.query("context_roi_scores")[0]
        assert stored["row_kind"] == "try"
        assert stored["size_band"] == "S"
        assert stored["try_ordinal"] == 0
        assert stored["check_verdict"] is None

    def test_projecting_a_full_trial_row_stores_the_new_columns(self) -> None:
        db = InmemoryDatabaseAdapter()
        row = ModelAttemptReductionRow.model_validate(
            {
                **_legacy_row_dict(),
                "check_verdict": "fail_check",
                "row_kind": "control",
                "size_band": "M",
                "try_ordinal": 2,
                "diff_hash": "abc",
                "max_turns": 12,
                "try_timeout_s": 600.0,
                "generation_s": 1.5,
                "env_sync_s": 2.5,
                "check_s": 3.5,
            }
        )
        result = ModelContextRoiRunResult(run_id="run-001", rows=(row,))
        HANDLER.project(result, db)
        stored = db.query("context_roi_scores")[0]
        assert stored["check_verdict"] == "fail_check"
        assert stored["row_kind"] == "control"
        assert stored["size_band"] == "M"
        assert stored["try_ordinal"] == 2
        assert stored["diff_hash"] == "abc"
        assert stored["max_turns"] == 12
        assert stored["try_timeout_s"] == 600.0
        assert stored["generation_s"] == 1.5
        assert stored["env_sync_s"] == 2.5
        assert stored["check_s"] == 3.5
