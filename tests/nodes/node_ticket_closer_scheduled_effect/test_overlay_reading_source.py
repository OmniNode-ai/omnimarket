# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer schedule overlay's reading profile and its bound-profile fields (OMN-24301)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.ticket_closer import (
    EnumCloserReadingSource,
    ModelTicketCloserResult,
    ModelTicketCloserScheduleOverlay,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_ticket_closer_scheduled_effect/overlay.yaml"
)


def overlay(**over: object) -> ModelTicketCloserScheduleOverlay:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return ModelTicketCloserScheduleOverlay.model_validate({**data, **over})


def test_golden_the_fixture_reads_as_the_unread_profile() -> None:
    schedule = overlay()
    assert schedule.reading_source is EnumCloserReadingSource.UNREAD
    assert schedule.home_env is None
    assert schedule.bus_lane is None
    assert schedule.ticket_timeout_seconds is None
    assert schedule.scan_limit is None


def test_the_contract_clones_profile_takes_the_bound_fields() -> None:
    schedule = overlay(
        reading_source="contract_clones",
        home_env="OMNI_HOME",
        bus_lane="ticket-closer",
        ticket_timeout_seconds=30.0,
        scan_limit=5,
    )
    assert schedule.reading_source is EnumCloserReadingSource.CONTRACT_CLONES
    assert schedule.home_env == "OMNI_HOME"
    assert schedule.bus_lane == "ticket-closer"
    assert schedule.ticket_timeout_seconds == 30.0
    assert schedule.scan_limit == 5


def test_error_an_unknown_reading_source_is_refused() -> None:
    with pytest.raises(ValueError, match="reading_source"):
        overlay(reading_source="telepathy")


def test_error_a_non_positive_scan_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="scan_limit"):
        overlay(scan_limit=0)


def test_error_a_non_positive_ticket_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="ticket_timeout_seconds"):
        overlay(ticket_timeout_seconds=0.0)


def test_the_result_carries_an_unread_reason_defaulting_to_empty() -> None:
    result = ModelTicketCloserResult.model_validate(
        {
            "tick_id": "tick-1",
            "computed_at": "2026-11-13T12:00:00Z",
            "candidates_read": False,
            "examined": 0,
            "flipped": 0,
            "outcomes": [],
        }
    )
    assert result.unread_reason == ""
