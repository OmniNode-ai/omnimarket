# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 and AC3: the build series and the floor ratchet."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_compaction_ab.handlers.handler_build_series import (
    InMemoryBuildSeries,
    ModelBuildSeriesEntry,
    check_floor,
)
from omnimarket.nodes.node_projection_compaction_ab.models import (
    ModelFloorFile,
    load_floor_file,
)

NOW = datetime(2026, 11, 2, tzinfo=UTC)
FLOOR = ModelFloorFile.model_validate(
    {
        "helps_verdict_seen": True,
        "criteria": {
            "h1_recall": {"direction": "higher_is_better", "floor": 0.8},
            "acted_share": {"direction": "higher_is_better", "floor": 0.5},
            "contradiction_rate": {"direction": "lower_is_better", "floor": 0.1},
            "cost_ratio": {"direction": "lower_is_better", "floor": 1.2},
        },
    }
)
GOOD = {
    "h1_recall": 0.8,
    "acted_share": 0.6,
    "contradiction_rate": 0.1,
    "cost_ratio": 1.0,
}


def _entry(scores: dict[str, float], **kw: str) -> ModelBuildSeriesEntry:
    return ModelBuildSeriesEntry.model_validate(
        {
            "candidate_id": "c1",
            "node_commit": "abc",
            "split": "calibration",
            "scores": scores,
            "recorded_at": NOW,
            **kw,
        }
    )


def test_candidate_id_required() -> None:
    for bad in ({"candidate_id": ""}, {"candidate_id": None}):
        with pytest.raises(ValidationError):
            ModelBuildSeriesEntry.model_validate(
                {
                    "node_commit": "abc",
                    "split": "calibration",
                    "scores": {},
                    "recorded_at": NOW,
                    **bad,
                }
            )
    with pytest.raises(ValidationError):
        ModelBuildSeriesEntry.model_validate(
            {
                "node_commit": "abc",
                "split": "calibration",
                "scores": {},
                "recorded_at": NOW,
            }
        )


def test_series_calibration_split_only_and_one_row_per_build() -> None:
    with pytest.raises(ValidationError):
        _entry(GOOD, split="held_out")
    series = InMemoryBuildSeries()
    appended = series.append(_entry(GOOD))
    duplicate_appended = series.append(_entry({**GOOD, "h1_recall": 0.1}))
    assert appended
    assert not duplicate_appended
    assert len(series.entries()) == 1


def test_floor_ratchet_both_directions() -> None:
    assert check_floor(_entry(GOOD), FLOOR) == ()
    assert (
        check_floor(_entry({**GOOD, "h1_recall": 0.9, "cost_ratio": 0.5}), FLOOR) == ()
    )
    for name, worse in (
        ("h1_recall", 0.79),
        ("acted_share", 0.1),
        ("contradiction_rate", 0.11),
        ("cost_ratio", 1.3),
    ):
        violations = check_floor(_entry({**GOOD, name: worse}), FLOOR)
        assert [v.criterion for v in violations] == [name], name


def test_floor_ratchet_missing_score_is_not_a_pass() -> None:
    scores = {k: v for k, v in GOOD.items() if k != "h1_recall"}
    assert [v.criterion for v in check_floor(_entry(scores), FLOOR)] == ["h1_recall"]


def test_floor_ratchet_informational_until_first_helps_verdict() -> None:
    off = FLOOR.model_copy(update={"helps_verdict_seen": False})
    assert check_floor(_entry({**GOOD, "h1_recall": 0.0}), off) == ()


def test_floor_ratchet_shipped_file_directions() -> None:
    floor = load_floor_file()
    higher = {"h1_recall", "acted_share", "planted_recall"}
    lower = {
        "contradiction_rate",
        "false_memory_rate",
        "hold_and_scope_violation_rate",
        "cost_ratio",
    }
    assert set(floor.criteria) == higher | lower
    for name, criterion in floor.criteria.items():
        assert criterion.direction.value == (
            "higher_is_better" if name in higher else "lower_is_better"
        )


def test_build_series_handler_dedupes_and_ratchets() -> None:
    from omnimarket.nodes.node_projection_compaction_ab.handlers.handler_build_series import (
        HandlerBuildSeries,
        ModelBuildSeriesRequest,
    )

    first = _entry(GOOD)
    handler = HandlerBuildSeries()
    result = handler.handle(ModelBuildSeriesRequest(entry=first))
    assert result.appended
    again = handler.handle(ModelBuildSeriesRequest(entry=first, existing=(first,)))
    assert not again.appended
