"""Tests for Training Data model."""

from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_training_data import (
    EnumLabelSource,
    ModelTrainingDataRecord,
)


def test_training_record_round_trips():
    record = ModelTrainingDataRecord(
        id=uuid4(),
        correlation_id=uuid4(),
        session_id=uuid4(),
        model_key="qwen3-coder",
        category=EnumFindingCategory.SECURITY,
        severity=EnumFindingSeverity.CRITICAL,
        code_diff_hash="abc123",
        prompt_hash="def456",
        model_response_hash="ghi789",
        local_detected=True,
        frontier_detected=True,
        label_source=EnumLabelSource.FRONTIER_BOOTSTRAP,
        recorded_at=datetime.now(tz=UTC),
    )
    data = record.model_dump(mode="json")
    rebuilt = ModelTrainingDataRecord.model_validate(data)
    assert rebuilt.model_key == "qwen3-coder"
    assert rebuilt.label_source == EnumLabelSource.FRONTIER_BOOTSTRAP
