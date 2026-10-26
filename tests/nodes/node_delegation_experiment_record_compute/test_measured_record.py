# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The first measured record of the experiment program passes the check and re-derives from its runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.delegation_experiment_record.model_experiment_record import (
    ModelExperimentRecord,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record_request import (
    ModelExperimentRecordRequest,
)
from omnimarket.nodes.node_delegation_experiment_record_compute.handlers.handler_delegation_experiment_record import (
    HandlerDelegationExperimentRecord,
)

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "m4_structured_brief_extraction.json"
_MIN_EFFECT = 0.15


def _fixture() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return loaded


def test_preregistration_text_matches_its_recorded_hash() -> None:
    fx = _fixture()
    assert hashlib.sha256(fx["prereg"].encode()).hexdigest() == fx["prereg_sha256"]


def test_record_passes_the_check_with_its_runs_as_receipts() -> None:
    fx = _fixture()
    record = ModelExperimentRecord.model_validate(fx["record"])
    request = ModelExperimentRecordRequest(
        retired_models=("retired-test-model",),
        listed_experiment_ids=(record.experiment_id,),
        records=(record,),
        known_receipt_ids=tuple(run["run_id"] for run in fx["runs"]),
    )
    result = HandlerDelegationExperimentRecord().handle(request)
    assert result.issues == ()
    assert [(c.experiment_id, c.run_count) for c in result.run_counts] == [
        (record.experiment_id, 32)
    ]


def test_record_without_the_runs_table_receipts_is_refused() -> None:
    fx = _fixture()
    record = ModelExperimentRecord.model_validate(fx["record"])
    request = ModelExperimentRecordRequest(
        retired_models=("retired-test-model",),
        listed_experiment_ids=(record.experiment_id,),
        records=(record,),
    )
    result = HandlerDelegationExperimentRecord().handle(request)
    assert len(result.issues) == 32


def test_per_arm_values_and_verdict_rederive_from_the_runs() -> None:
    fx = _fixture()
    record = fx["record"]
    for arm in record["arms"]:
        rows = [run for run in fx["runs"] if run["arm"] == arm["arm_id"]]
        assert [run["run_id"] for run in rows] == arm["run_ids"]
        assert all(run["models"] == ["Qwen3.8-27B"] for run in rows)
        assert arm["per_arm_value"] == sum(run["correct"] for run in rows) / len(rows)
    by_arm = {arm["arm_id"]: arm["per_arm_value"] for arm in record["arms"]}
    assert record["baseline"] == by_arm["A"]
    supported = by_arm["B"] - by_arm["A"] >= _MIN_EFFECT
    assert record["verdict"] == ("supported" if supported else "refuted")
