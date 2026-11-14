# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24177: the node's overlay carries every setting, and a node without one boots and refuses on its tick."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.eval_tracking.model_eval_tracking_overlay import (
    ModelEvalTrackingOverlay,
)
from omnimarket.nodes.node_eval_tracking_schedule_compute.handlers import (
    HandlerEvalTrackingSchedule,
)
from tests.nodes.node_eval_tracking_schedule_compute.support import (
    EPOCH,
    tick_at,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_eval_tracking_schedule_compute"
)


def test_the_shipped_example_overlay_validates() -> None:
    import yaml

    document = yaml.safe_load((NODE / "overlay.example.yaml").read_text())
    overlay = ModelEvalTrackingOverlay.model_validate(document)
    assert (overlay.local_days, overlay.glm_days) == (7, 28)
    assert len(overlay.record.candidates) == 3


def test_the_overlay_is_read_from_the_overlay_root_on_the_first_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "node_eval_tracking_schedule_compute"
    target.mkdir()
    (target / "overlay.yaml").write_text((NODE / "overlay.example.yaml").read_text())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    handler = HandlerEvalTrackingSchedule()
    named = [
        handler.handle(tick_at(EPOCH + dt.timedelta(seconds=900 * i))) for i in range(4)
    ]
    models = [r.arms[0].model for r in named if r is not None]
    assert models == [
        "example-local-model",
        "example-hosted-model",
        "example-local-model",
        "example-hosted-model",
    ] or models == [
        "example-hosted-model",
        "example-local-model",
        "example-hosted-model",
        "example-local-model",
    ], "the overlay's resend interval hands the turn to each arm in order"


def test_no_overlay_boots_the_handler_and_refuses_on_the_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    handler = HandlerEvalTrackingSchedule()
    with pytest.raises(NodeOverlayNotFoundError):
        handler.handle(tick_at(EPOCH))
