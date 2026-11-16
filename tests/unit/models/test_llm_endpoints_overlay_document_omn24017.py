# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/llm_endpoints/overlay.yaml through (OMN-24024)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_llm_endpoints_overlay import (
    ModelLlmEndpointsOverlay,
)

pytestmark = pytest.mark.unit

VALID_URL = "http://192.0.2.10:8100"


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "endpoint_urls": {"slot_a": VALID_URL}},
        {"schema_version": 1, "endpoint_urls": {"slot_a": "http://192.0.2.10:8100/"}},
        {"schema_version": 1, "endpoint_urls": {"slot_a": "https://192.0.2.10:8100"}},
    ],
)
def test_a_valid_document_passes(document: dict[str, object]) -> None:
    overlay = ModelLlmEndpointsOverlay.model_validate(document)
    assert overlay.schema_version == 1
    assert overlay.endpoint_urls


def test_trailing_slash_is_stripped() -> None:
    overlay = ModelLlmEndpointsOverlay.model_validate(
        {"schema_version": 1, "endpoint_urls": {"slot_a": "http://192.0.2.10:8100/"}}
    )
    assert overlay.endpoint_urls["slot_a"] == "http://192.0.2.10:8100"


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "endpoint_urls": {"slot_a": VALID_URL}, "stray": 1},
        {"schema_version": 2, "endpoint_urls": {"slot_a": VALID_URL}},
        {"schema_version": 1, "endpoint_urls": {}},
        {"schema_version": 1, "endpoint_urls": {"slot_a": "http://192.0.2.10"}},
        {
            "schema_version": 1,
            "endpoint_urls": {"slot_a": "http://192.0.2.10:8100/path"},
        },
        {
            "schema_version": 1,
            "endpoint_urls": {"slot_a": "http://192.0.2.10:8100?x=1"},
        },
        {"schema_version": 1, "endpoint_urls": {"slot_a": "ftp://192.0.2.10:8100"}},
        {"schema_version": 1, "endpoint_urls": {"": VALID_URL}},
        {"schema_version": 1, "endpoint_urls": {"slot a": VALID_URL}},
    ],
)
def test_a_wrong_document_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelLlmEndpointsOverlay.model_validate(document)
