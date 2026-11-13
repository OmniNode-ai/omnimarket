# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23895: invalid inputs are refused by validation; the handler never sees them."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_store_adopt_compute.models import (
    ModelHeldLabel,
    ModelStoreAdoptPolicy,
    ModelStoreAdoptRequest,
    ModelStoreToken,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "prefix", ["", "claude_", "CLAUDE", "1CLAUDE_", "CLAUDE-OAUTH-"]
)
def test_a_bad_key_prefix_is_refused(prefix: str) -> None:
    with pytest.raises(ValidationError):
        ModelStoreAdoptPolicy(key_prefix=prefix)


@pytest.mark.parametrize("label", ["", "Upper", "-lead", "has space", "a" * 64])
def test_a_bad_label_is_refused(label: str) -> None:
    with pytest.raises(ValidationError):
        ModelHeldLabel(host="h053", label=label, fingerprint="aaaaaaaa")
    with pytest.raises(ValidationError):
        ModelStoreToken(label=label, key="K")


def test_an_empty_fingerprint_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHeldLabel(host="h053", label="x", fingerprint="")


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelStoreAdoptRequest.model_validate({"held": [], "unexpected": 1})
