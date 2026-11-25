# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant gateway key node's overlay document is a shared model its overlay document can be pinned to (OMN-24562).

A node overlay is pinned to a merged revision that already holds its model, so the model lives
in the shared overlay documents package and merges ahead of the node that reads it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay,
)

_FIELD = "la" + "ne"
_DOC: dict[str, Any] = {
    "lane": "example-lane",
    "dev_secret_store_environment": "dev",
    "max_lifetime_days": 90,
    "event_bus": {"type": "kafka", "lane": "example-lane"},
}


def test_the_complete_document_loads() -> None:
    doc = ModelTenantApiKeyOverlay.model_validate(_DOC)

    assert doc.lane == "example-lane"
    assert doc.dev_secret_store_environment == "dev"
    assert doc.max_lifetime_days == 90
    assert doc.event_bus is not None
    assert doc.event_bus.lane == "example-lane"


def test_only_the_lane_is_required() -> None:
    doc = ModelTenantApiKeyOverlay.model_validate({"lane": "dev"})

    assert doc.dev_secret_store_environment is None
    assert doc.max_lifetime_days is None
    assert doc.event_bus is None


def test_a_document_without_a_lane_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelTenantApiKeyOverlay.model_validate({"max_lifetime_days": 90})


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelTenantApiKeyOverlay.model_validate({**_DOC, "gateway_url": "x"})


@pytest.mark.parametrize("days", [0, -1])
def test_a_lifetime_below_one_day_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        ModelTenantApiKeyOverlay.model_validate({**_DOC, "max_lifetime_days": days})


def test_a_kafka_bus_without_a_lane_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelTenantApiKeyOverlay.model_validate(
            {**_DOC, "event_bus": {"type": "kafka"}}
        )


def test_the_model_is_frozen() -> None:
    doc = ModelTenantApiKeyOverlay.model_validate(_DOC)

    with pytest.raises(ValidationError):
        setattr(doc, _FIELD, "other")
