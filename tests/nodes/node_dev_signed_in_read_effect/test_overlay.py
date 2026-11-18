# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev signed-in read overlay model (OMN-24345): the example loads, and every deployment fact is required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dev_signed_in_read_effect.overlay import (
    ModelDevSignedInReadOverlay,
)

from ._support import overlay, overlay_document

pytestmark = pytest.mark.unit


def test_example_overlay_loads() -> None:
    loaded = overlay()
    assert loaded.lane == "dev"
    assert loaded.read.data_states == ("found", "not_found", "projection_absent")


@pytest.mark.parametrize("block", ["lane", "keycloak", "user", "api", "read"])
def test_a_missing_block_is_refused(block: str) -> None:
    document = overlay_document()
    del document[block]
    with pytest.raises(ValidationError):
        ModelDevSignedInReadOverlay.model_validate(document)


@pytest.mark.parametrize(
    ("block", "field"),
    [
        ("keycloak", "base_url"),
        ("keycloak", "realm"),
        ("user", "password_key"),
        ("api", "base_url"),
        ("read", "path"),
        ("read", "correlation_id"),
        ("read", "data_states"),
    ],
)
def test_a_missing_field_is_refused_not_filled(block: str, field: str) -> None:
    document = overlay_document()
    del document[block][field]
    with pytest.raises(ValidationError):
        ModelDevSignedInReadOverlay.model_validate(document)


def test_an_unknown_field_is_refused() -> None:
    document = overlay_document()
    document["read"]["method"] = "POST"
    with pytest.raises(ValidationError):
        ModelDevSignedInReadOverlay.model_validate(document)
