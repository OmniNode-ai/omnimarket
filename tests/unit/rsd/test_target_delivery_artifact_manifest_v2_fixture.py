# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable public fixture integrity for the B2 V2 validator."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_VECTORS = Path(__file__).parents[3] / "src/omnimarket/rsd/vectors"
_FIXTURES = {
    "target_delivery_artifact_manifest_public_vector.yaml": (
        "4b10ec2b37f0768d0a8fa283d5a26cc6020e26a968ebb3928b78d4b8f73c65ed",
        339_553,
        4_174,
    ),
    "target_delivery_artifact_manifest_v2_public_vector.yaml": (
        "5b91cfb95243403b3ddc234d154df355f46787f0c3ea49613d5bf404b1a72237",
        52_693,
        667,
    ),
    "container_bootstrap_artifact_evidence_v5_public_vector.yaml": (
        "6c66df411fd080f1d20e2cfe8f8004f600a1cfe1fe5942259b148af15166ca91",
        238_294,
        2_948,
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize(("filename", "expected"), _FIXTURES.items())
def test_b2_v2_public_fixtures_are_exact_immutable_snapshots(
    filename: str, expected: tuple[str, int, int]
) -> None:
    """Pin every V2 input snapshot before the nested validator consumes it."""

    digest, byte_count, line_count = expected
    payload = (_VECTORS / filename).read_bytes()

    assert payload.isascii()
    assert hashlib.sha256(payload).hexdigest() == digest
    assert len(payload) == byte_count
    assert payload.count(b"\n") == line_count
