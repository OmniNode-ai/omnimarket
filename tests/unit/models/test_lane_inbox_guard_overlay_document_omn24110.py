# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/lane_inbox_guard/overlay.yaml through (OMN-24110)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_lane_inbox_guard_overlay import (
    LANE_INBOX_GUARD_OVERLAY_KEYS,
    ModelLaneInboxGuardOverlay,
)

pytestmark = pytest.mark.unit

EXPECTED_KEYS = frozenset(
    {
        "reader",
        "reader_module",
        "interpreter_env",
        "lane_env",
        "state_file_name",
        "journal_file_name",
        "cache_file_name",
        "refresh_log_name",
        "refresher_script",
        "fallback_inbox_script",
        "hold_push_pattern",
        "outbox_env",
        "outbox_append_script",
        "outbox_delivered_file_name",
        "window_hours",
        "interval_s",
        "timeout_s",
        "cold_wait_s",
        "max_listed",
        "outbox_timeout_s",
        "outbox_kinds",
        "skip_hold_cells",
    }
)

REQUIRED_KEYS = tuple(
    k for k in LANE_INBOX_GUARD_OVERLAY_KEYS if k != "skip_hold_cells"
)

INT_KEYS = (
    "window_hours",
    "interval_s",
    "timeout_s",
    "cold_wait_s",
    "max_listed",
    "outbox_timeout_s",
)


def _golden() -> dict[str, object]:
    return {
        "reader": "lane-inbox",
        "reader_module": "pkg.reader",
        "interpreter_env": "OMNI_INTERPRETER",
        "lane_env": "OMNI_LANE",
        "state_file_name": "state.json",
        "journal_file_name": "journal.jsonl",
        "cache_file_name": "cache.json",
        "refresh_log_name": "refresh.log",
        "refresher_script": "refresh.py",
        "fallback_inbox_script": "fallback_inbox.py",
        "hold_push_pattern": r"\bgit push\b",
        "outbox_env": "OMNI_OUTBOX",
        "outbox_append_script": "outbox_append.py",
        "outbox_delivered_file_name": "delivered.jsonl",
        "window_hours": 2,
        "interval_s": 60,
        "timeout_s": 30,
        "cold_wait_s": 15,
        "max_listed": 5,
        "outbox_timeout_s": 120,
        "outbox_kinds": ["MSG", "ACK"],
        "skip_hold_cells": ["surface"],
    }


def test_a_golden_document_passes() -> None:
    overlay = ModelLaneInboxGuardOverlay.model_validate(_golden())
    assert overlay.reader == "lane-inbox"
    assert overlay.window_hours == 2
    assert overlay.outbox_kinds == ("MSG", "ACK")
    assert overlay.skip_hold_cells == ("surface",)


def test_skip_hold_cells_may_be_absent() -> None:
    document = _golden()
    del document["skip_hold_cells"]
    overlay = ModelLaneInboxGuardOverlay.model_validate(document)
    assert overlay.skip_hold_cells == ()


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_a_missing_required_key_is_refused(key: str) -> None:
    document = _golden()
    del document[key]
    with pytest.raises(ValidationError):
        ModelLaneInboxGuardOverlay.model_validate(document)


def test_an_unknown_key_is_refused() -> None:
    document = _golden()
    document["stray"] = 1
    with pytest.raises(ValidationError):
        ModelLaneInboxGuardOverlay.model_validate(document)


@pytest.mark.parametrize("key", INT_KEYS)
@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_a_bad_int_value_is_refused(key: str, bad: object) -> None:
    document = _golden()
    document[key] = bad
    with pytest.raises(ValidationError):
        ModelLaneInboxGuardOverlay.model_validate(document)


def test_an_empty_outbox_kinds_is_refused() -> None:
    document = _golden()
    document["outbox_kinds"] = []
    with pytest.raises(ValidationError):
        ModelLaneInboxGuardOverlay.model_validate(document)


def test_an_uncompilable_hold_push_pattern_is_refused() -> None:
    document = _golden()
    document["hold_push_pattern"] = "("
    with pytest.raises(ValidationError):
        ModelLaneInboxGuardOverlay.model_validate(document)


def test_the_model_fields_equal_the_expected_set() -> None:
    assert set(ModelLaneInboxGuardOverlay.model_fields) == EXPECTED_KEYS
