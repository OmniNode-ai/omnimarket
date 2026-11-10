# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 7 (OMN-23321): the measured windows reach the overlay threshold, and a burst stops only its origin."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_andon_threshold import (
    ModelBurstObservation,
    ModelBurstScopeRequest,
    ModelClassEvent,
    ModelMeasurementProvenance,
    ModelThresholdComputeRequest,
    ModelWindowMeasureRequest,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_burst_scope import (
    HandlerBurstScope,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_threshold_compute import (
    HandlerThresholdCompute,
)
from omnimarket.nodes.node_andon_threshold_compute.handlers.handler_window_measure import (
    HandlerWindowMeasure,
)
from omnimarket.nodes.node_andon_threshold_compute.overlay import (
    OVERLAY_ID,
    load_threshold_freeze,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
RECALIBRATED = date(2026, 11, 9)
PIECES = (1, 2, 3, 7, 9)


def _recorded_friction() -> tuple[ModelClassEvent, ...]:
    # hook.refusal: a burst of 4 inside one day. ci.timeout: one event. Three days recorded.
    hooks = tuple(
        ModelClassEvent(class_id="hook.refusal", observed_at=T0 + timedelta(hours=h))
        for h in (5.0, 5.1, 5.2, 5.3)
    )
    return (
        *hooks,
        ModelClassEvent(class_id="ci.timeout", observed_at=T0 + timedelta(hours=30)),
    )


def _measured_overlay_text() -> str:
    events = _recorded_friction()
    measured = HandlerWindowMeasure().handle(
        ModelWindowMeasureRequest(
            events=events,
            measured_from=T0,
            measured_until=T0 + timedelta(days=3),
            step_hours=1,
        )
    )
    return (
        HandlerThresholdCompute()
        .handle(
            ModelThresholdComputeRequest(
                classes=measured.classes,
                recalibrated_on=RECALIBRATED,
                measurement=ModelMeasurementProvenance(
                    source="test friction record",
                    measured_from=T0,
                    measured_until=T0 + timedelta(days=3),
                    step_hours=1,
                    events_read=measured.events_read,
                ),
            )
        )
        .overlay_yaml
    )


def _overlay_root(tmp_path: Path, document: dict[str, object] | None) -> dict[str, str]:
    root = tmp_path / OVERLAY_ID
    root.mkdir()
    if document is not None:
        (root / "overlay.yaml").write_text(yaml.safe_dump(document))
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_measure_windows_threshold_is_read_from_the_overlay_with_hash_and_date(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(_measured_overlay_text())
    freeze = load_threshold_freeze(_overlay_root(tmp_path, document))
    assert freeze.thresholds == {"ci.timeout": 1, "hook.refusal": 4}
    assert freeze.threshold_hash == document["threshold_hash"]
    assert len(freeze.threshold_hash) == 64
    assert freeze.recalibrated_on == RECALIBRATED


def test_measure_windows_overlay_value_removed_is_refused(tmp_path: Path) -> None:
    # The failing control: the measured class loses its overlay value and the old hash stays.
    document = yaml.safe_load(_measured_overlay_text())
    del document["thresholds"]["hook.refusal"]
    with pytest.raises(NodeOverlayError):
        load_threshold_freeze(_overlay_root(tmp_path, document))


def test_measure_windows_overlay_absent_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError):
        load_threshold_freeze(_overlay_root(tmp_path, None))


def _count_24h(
    events: tuple[ModelClassEvent, ...], class_id: str, now: datetime
) -> int:
    return sum(
        1
        for e in events
        if e.class_id == class_id and now - timedelta(hours=24) < e.observed_at <= now
    )


def _tick_observations(
    burst_pieces: dict[int, int], now: datetime
) -> tuple[ModelBurstObservation, ...]:
    # Each piece records its own hook.refusal events in the tick; the count is that piece's alone.
    return tuple(
        ModelBurstObservation(
            piece=piece,
            class_id="hook.refusal",
            count_24h=_count_24h(
                tuple(
                    ModelClassEvent(
                        class_id="hook.refusal", observed_at=now - timedelta(minutes=n)
                    )
                    for n in range(count)
                ),
                "hook.refusal",
                now,
            ),
        )
        for piece, count in burst_pieces.items()
    )


def _scope(
    tmp_path: Path, observations: tuple[ModelBurstObservation, ...]
) -> ModelBurstScopeRequest:
    freeze = load_threshold_freeze(
        _overlay_root(tmp_path, yaml.safe_load(_measured_overlay_text()))
    )
    return ModelBurstScopeRequest(
        freeze=freeze, pieces=PIECES, observations=observations
    )


def test_andon_burst_stops_origin_and_leaves_every_other_piece_admitted(
    tmp_path: Path,
) -> None:
    now = T0 + timedelta(days=4)
    # Piece 3 plants 5 refusals against a measured peak of 4; piece 7 stays at the peak.
    request = _scope(tmp_path, _tick_observations({3: 5, 7: 4}, now))
    result = HandlerBurstScope().handle(request)
    assert result.stopped_pieces == (3,)
    assert result.admitted_pieces == (1, 2, 7, 9)


def test_andon_burst_stops_origin_not_the_pieces_that_share_the_class(
    tmp_path: Path,
) -> None:
    # The failing control: two pieces each at 3 share the class (sum 6 > 4). Judged by the
    # sum, both would stop; judged per originating piece, neither does.
    now = T0 + timedelta(days=4)
    request = _scope(tmp_path, _tick_observations({1: 3, 2: 3}, now))
    result = HandlerBurstScope().handle(request)
    assert result.stopped_pieces == ()
    assert sum(o.count_24h for o in request.observations) > 4


def test_andon_burst_stops_origin_in_one_evaluation(tmp_path: Path) -> None:
    now = T0 + timedelta(days=4)
    request = _scope(tmp_path, _tick_observations({9: 6}, now))
    handler = HandlerBurstScope()
    first = handler.handle(request)
    assert first.stopped_pieces == (9,)
    assert handler.handle(request) == first
