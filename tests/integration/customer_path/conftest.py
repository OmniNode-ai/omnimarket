# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18012 customer-path fixtures: one auth-required broker per session.

``OMN18012_REQUIRE_HARNESS=1`` (set by the CI job) turns a missing docker
daemon into a hard error instead of a skip. Locally a developer without
docker still gets a skip; in CI the boundary either runs or the job is red.
A boundary test that can quietly not-run is how the 2026-09-06 escapes stayed
invisible.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.integration.customer_path.redpanda_sasl_harness import (
    RedpandaSasl,
    docker_available,
    start_redpanda_sasl,
    stop_redpanda,
)


@pytest.fixture(scope="session")
def redpanda_sasl() -> Iterator[RedpandaSasl]:
    """An auth-required Redpanda broker, torn down at session end."""
    required = os.environ.get("OMN18012_REQUIRE_HARNESS") == "1"
    if not docker_available():
        if required:
            pytest.fail(
                "OMN18012_REQUIRE_HARNESS=1 but no docker daemon is reachable; "
                "the customer-path boundary tests did not run"
            )
        pytest.skip("docker daemon unavailable (local developer skip)")
    broker = start_redpanda_sasl()
    try:
        yield broker
    finally:
        stop_redpanda(broker)
