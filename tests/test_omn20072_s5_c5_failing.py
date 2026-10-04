# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 constructed case C5 (OMN-20072): the bound test fails, whatever the PR body says."""

import pytest

pytestmark = pytest.mark.unit


def test_omn20072_s5_c5_failing() -> None:
    assert sum([1, 2, 3]) == 7, "constructed failure: the bound check must refuse"
