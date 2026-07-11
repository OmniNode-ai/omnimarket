# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for HandlerContractDigest.

Pins the digest contract: same input yields the same digest (stable), different
input yields a different digest (input-sensitive), and the digest equals a plain
sha256 over the UTF-8 contract bytes.
"""

from __future__ import annotations

import hashlib

import pytest

from omnimarket.contract_assembly.models import ModelContractDigestRequest
from omnimarket.nodes.node_contract_digest_compute.handlers.handler_contract_digest import (
    HandlerContractDigest,
)


def _digest(contract_yaml: str) -> str:
    return (
        HandlerContractDigest()
        .handle(ModelContractDigestRequest(contract_yaml=contract_yaml))
        .contract_sha256
    )


@pytest.mark.unit
class TestContractDigest:
    def test_digest_is_stable_for_the_same_input(self) -> None:
        assert _digest("metadata: {}\n") == _digest("metadata: {}\n")

    def test_digest_is_input_sensitive(self) -> None:
        assert _digest("a: 1\n") != _digest("a: 2\n")

    def test_digest_equals_sha256_of_utf8_bytes(self) -> None:
        text = "metadata:\n  node_name: NodeFooCompute\n"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert _digest(text) == expected

    def test_digest_is_hex_sha256_length(self) -> None:
        assert len(_digest("anything\n")) == 64

    def test_whitespace_change_changes_the_digest(self) -> None:
        assert _digest("a: 1\n") != _digest("a: 1\n\n")
