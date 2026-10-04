# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 constructed case C8 (OMN-20072): a child unit test that passes."""

import pytest

pytestmark = pytest.mark.unit


def test_omn20072_s5_c8_unit_a() -> None:
    assert sorted([3, 1, 2]) == [1, 2, 3]
