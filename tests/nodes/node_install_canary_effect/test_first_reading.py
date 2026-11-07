# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The first install-canary result is dated, for the floor's first_reading (OMN-22954)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.models.floor_17a.model_floor_17a import ModelPieceDef
from omnimarket.nodes.node_install_canary_effect.handlers.handler_install_canary import (
    HandlerInstallCanary,
    ModelInstallCanaryRequest,
)

pytestmark = pytest.mark.unit


def _event(ts: str, verdict: str, host: str = "h200") -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "canary",
        "host": host,
        "plugin": "omni",
        "version": "1.4.0",
        "verdict": verdict,
        "sha": "a" * 40,
    }


def _first_reading(events: list[dict[str, Any]]) -> str:
    return str(
        HandlerInstallCanary()
        .handle(ModelInstallCanaryRequest(operation="first_reading", args=(events,)))
        .value
    )


def test_first_reading_is_the_earliest_canary_result_with_its_date() -> None:
    reading = _first_reading(
        [
            _event("2026-11-04T09:00:00Z", "pass"),
            {"ts": "2026-11-01T00:00:00Z", "kind": "adopt", "host": "h200"},
            _event("2026-11-02T05:06:00Z", "fail", host="h101"),
        ]
    )
    assert reading == "2026-11-02 fail omni 1.4.0 on h101"


def test_first_reading_is_empty_when_no_canary_ran() -> None:
    assert _first_reading([]) == ""
    assert _first_reading([{"ts": "2026-11-01T00:00:00Z", "kind": "adopt"}]) == ""


def test_first_reading_fills_the_floor_piece_declaration() -> None:
    reading = _first_reading([_event("2026-11-02T05:06:00Z", "pass")])
    piece = ModelPieceDef(
        piece=9, name="install canary", reading_max_age_s=3600, first_reading=reading
    )
    assert piece.first_reading.startswith("2026-11-02 pass")
    assert json.loads(piece.model_dump_json())["first_reading"] == reading
