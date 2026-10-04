# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 constructed case C8 (OMN-20072): the parent integration test composes the two children and fails."""

import pytest

pytestmark = pytest.mark.unit


def test_omn20072_s5_c8_integration() -> None:
    assert "-".join(sorted(["b", "a"])) == "b-a", (
        "constructed failure: the integration of green children is broken"
    )
