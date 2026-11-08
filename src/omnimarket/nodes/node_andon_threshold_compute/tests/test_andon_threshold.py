# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 7: frozen per-class thresholds (OMN-22942 AC1) and the scope of a friction burst (AC2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_andon_threshold import (
    ModelAndonThresholdFreeze,
    ModelBurstObservation,
    ModelBurstScopeRequest,
    ModelClassWindowCounts,
    ModelThresholdComputeRequest,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_burst_scope import (
    HandlerBurstScope,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_threshold_compute import (
    HandlerThresholdCompute,
)
from omnimarket.nodes.node_andon_threshold_compute.overlay import (
    OVERLAY_ID,
    load_threshold_freeze,
)

pytestmark = pytest.mark.unit

RECALIBRATED = date(2026, 11, 7)


def _census() -> ModelThresholdComputeRequest:
    return ModelThresholdComputeRequest(
        classes=(
            ModelClassWindowCounts(class_id="hook.refusal", window_counts=(3, 17, 5)),
            ModelClassWindowCounts(class_id="ci.timeout", window_counts=(0, 2, 1)),
            ModelClassWindowCounts(class_id="lane.stall", window_counts=(9,)),
        ),
        recalibrated_on=RECALIBRATED,
    )


def _freeze() -> ModelAndonThresholdFreeze:
    return HandlerThresholdCompute().handle(_census()).freeze


def test_freeze_hash_thresholds_are_the_measured_peaks_with_date() -> None:
    result = HandlerThresholdCompute().handle(_census())
    assert result.freeze.thresholds == {
        "ci.timeout": 2,
        "hook.refusal": 17,
        "lane.stall": 9,
    }
    assert result.freeze.recalibrated_on == RECALIBRATED
    assert len(result.freeze.threshold_hash) == 64


def test_freeze_hash_is_deterministic_and_follows_the_values() -> None:
    again = HandlerThresholdCompute().handle(_census()).freeze
    assert again.threshold_hash == _freeze().threshold_hash
    other = HandlerThresholdCompute().handle(
        _census().model_copy(
            update={
                "classes": (
                    ModelClassWindowCounts(
                        class_id="hook.refusal", window_counts=(18,)
                    ),
                    *_census().classes[1:],
                )
            }
        )
    )
    assert other.freeze.threshold_hash != _freeze().threshold_hash


def test_freeze_hash_changed_value_without_new_hash_fails() -> None:
    frozen = _freeze()
    tampered = {**frozen.thresholds, "hook.refusal": 999}
    with pytest.raises(ValidationError, match="threshold_hash"):
        ModelAndonThresholdFreeze(
            thresholds=tampered,
            threshold_hash=frozen.threshold_hash,
            recalibrated_on=RECALIBRATED,
        )


def test_freeze_hash_overlay_text_round_trips_and_loads(tmp_path: Path) -> None:
    result = HandlerThresholdCompute().handle(_census())
    root = tmp_path / OVERLAY_ID
    root.mkdir()
    (root / "overlay.yaml").write_text(result.overlay_yaml)
    loaded = load_threshold_freeze({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded == result.freeze


def test_freeze_hash_overlay_with_edited_value_refuses_to_load(tmp_path: Path) -> None:
    document = yaml.safe_load(HandlerThresholdCompute().handle(_census()).overlay_yaml)
    document["thresholds"]["ci.timeout"] = 50
    root = tmp_path / OVERLAY_ID
    root.mkdir()
    (root / "overlay.yaml").write_text(yaml.safe_dump(document))
    with pytest.raises(NodeOverlayError):
        load_threshold_freeze({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_freeze_hash_refuses_duplicate_class_and_negative_count() -> None:
    with pytest.raises(ValidationError):
        ModelThresholdComputeRequest(
            classes=(
                ModelClassWindowCounts(class_id="a", window_counts=(1,)),
                ModelClassWindowCounts(class_id="a", window_counts=(2,)),
            ),
            recalibrated_on=RECALIBRATED,
        )
    with pytest.raises(ValidationError):
        ModelClassWindowCounts(class_id="a", window_counts=(-1,))


def _scope(*observations: ModelBurstObservation) -> ModelBurstScopeRequest:
    return ModelBurstScopeRequest(
        freeze=_freeze(), pieces=(1, 2, 3, 7, 9), observations=observations
    )


def test_burst_scope_planted_burst_stops_only_its_originating_piece() -> None:
    request = _scope(
        ModelBurstObservation(piece=3, class_id="hook.refusal", count_24h=18),
        ModelBurstObservation(piece=7, class_id="hook.refusal", count_24h=17),
        ModelBurstObservation(piece=9, class_id="ci.timeout", count_24h=1),
    )
    result = HandlerBurstScope().handle(request)
    assert result.stopped_pieces == (3,)
    assert result.admitted_pieces == (1, 2, 7, 9)
    stopped = next(d for d in result.decisions if d.piece == 3)
    assert [(b.class_id, b.count_24h, b.threshold) for b in stopped.breaches] == [
        ("hook.refusal", 18, 17)
    ]


def test_burst_scope_no_burst_admits_every_piece() -> None:
    result = HandlerBurstScope().handle(
        _scope(ModelBurstObservation(piece=1, class_id="lane.stall", count_24h=9))
    )
    assert result.stopped_pieces == ()
    assert result.admitted_pieces == (1, 2, 3, 7, 9)


def test_burst_scope_judges_each_piece_alone_not_the_sum() -> None:
    result = HandlerBurstScope().handle(
        _scope(
            ModelBurstObservation(piece=1, class_id="hook.refusal", count_24h=10),
            ModelBurstObservation(piece=2, class_id="hook.refusal", count_24h=10),
        )
    )
    assert result.stopped_pieces == ()


def test_burst_scope_unmeasured_class_is_named_not_read_as_within_bounds() -> None:
    result = HandlerBurstScope().handle(
        _scope(ModelBurstObservation(piece=2, class_id="never.measured", count_24h=500))
    )
    assert result.unmeasured_classes == ("never.measured",)
    assert result.stopped_pieces == ()


def test_burst_scope_refuses_observation_of_unlisted_piece() -> None:
    with pytest.raises(ValidationError, match="not listed"):
        _scope(ModelBurstObservation(piece=18, class_id="ci.timeout", count_24h=9))
