# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant outbox flush schedule overlay document is a shared model an overlay can be pinned to (OMN-24562)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_tenant_outbox_flush_schedule_overlay import (
    ModelTenantOutboxFlushScheduleOverlay,
)

_DOC: dict[str, Any] = {
    "job_id": "tenant_outbox_flush",
    "run_interval_seconds": 60,
    "grace_seconds": 30,
}


def _doc() -> dict[str, Any]:
    return copy.deepcopy(_DOC)


def test_valid_document_is_accepted() -> None:
    overlay = ModelTenantOutboxFlushScheduleOverlay.model_validate(_doc())
    assert overlay.job_id == "tenant_outbox_flush"
    assert overlay.run_interval_seconds == 60
    assert overlay.grace_seconds == 30


@pytest.mark.parametrize("missing", ["job_id", "run_interval_seconds", "grace_seconds"])
def test_a_key_left_out_is_refused(missing: str) -> None:
    doc = _doc()
    del doc[missing]
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushScheduleOverlay.model_validate(doc)


def test_extra_key_is_refused() -> None:
    doc = _doc()
    doc["batch_limit"] = 50
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushScheduleOverlay.model_validate(doc)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("job_id", "Tenant-Outbox"),
        ("run_interval_seconds", 0),
        ("grace_seconds", -1),
    ],
)
def test_a_malformed_value_is_refused(key: str, value: Any) -> None:
    doc = _doc()
    doc[key] = value
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushScheduleOverlay.model_validate(doc)


def test_the_model_is_frozen() -> None:
    overlay = ModelTenantOutboxFlushScheduleOverlay.model_validate(_doc())
    with pytest.raises(ValidationError):
        overlay.grace_seconds = 5
