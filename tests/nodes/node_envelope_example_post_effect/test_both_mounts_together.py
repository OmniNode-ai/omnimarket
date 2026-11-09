# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both example-envelope contracts mount on one runtime without an overlap."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.integration.customer_path.redpanda_sasl_harness import RedpandaSasl
from tests.nodes import envelope_example_runtime as runtime
from tests.nodes import envelope_example_support as support

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def profile_broker() -> Iterator[RedpandaSasl | None]:
    yield from runtime.broker_for_profile()


async def test_both_routes_mount_together(
    profile_broker: RedpandaSasl | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.check_runtime_mount(support.ROUTES, profile_broker, monkeypatch)
