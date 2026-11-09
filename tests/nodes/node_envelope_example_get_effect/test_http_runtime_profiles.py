# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GET route through the runtime's real HTTP mount, in either profile.

ONEX_ENVELOPE_EXAMPLE_TEST_PROFILE=redpanda runs the same test beside an
isolated auth-required broker; see tests/nodes/envelope_example_runtime.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.integration.customer_path.redpanda_sasl_harness import RedpandaSasl
from tests.nodes import envelope_example_runtime as runtime
from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit
ROUTE = support.GET_ROUTE


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    yield from runtime.broker_for_profile()


async def test_runtime_mount_serves_the_route(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.check_runtime_mount((ROUTE,), profile_broker, monkeypatch)
