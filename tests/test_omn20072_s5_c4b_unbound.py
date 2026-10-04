# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S5 constructed case C4b (OMN-20072): a passing test that no criterion is bound to."""

import pytest

pytestmark = pytest.mark.unit


def test_omn20072_s5_c4b_runs_but_binds_nothing() -> None:
    assert sum([1, 2, 3]) == 6
