# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Digest helper tests for node_host_install_readback_compute (OMN-23957)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text

pytestmark = pytest.mark.unit


def test_sha256_of_abc_equals_the_known_value() -> None:
    assert (
        digest_text("abc", "sha256")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_unknown_algorithm_refuses_with_typed_value_error() -> None:
    with pytest.raises(ValueError, match="not-a-real-algo"):
        digest_text("abc", "not-a-real-algo")
