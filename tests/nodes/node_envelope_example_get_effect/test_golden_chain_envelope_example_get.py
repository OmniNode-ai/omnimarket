# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_envelope_example_get_effect: contract, dispatch, handler."""

from __future__ import annotations

import pytest

from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.integration


async def test_command_dispatch_builds_the_example_envelope() -> None:
    await support.check_golden_chain(support.GET_ROUTE)
