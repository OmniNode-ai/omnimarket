# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18012 customer-path fixtures: ONE auth-required broker per session.

Where the broker comes from is decided in one place --
``redpanda_sasl_harness.resolve_broker()`` -- and the order is the operator
ruling of 2026-09-07: a broker DECLARED in the environment is adopted and
nothing is started; a container is started only when nothing is declared.

``OMN18012_REQUIRE_HARNESS=1`` (set by the CI job) turns a missing docker
daemon into a hard error instead of a skip. Locally a developer with neither a
declared broker nor docker still gets a skip; in CI the boundary either runs or
the job is red. A boundary test that can quietly not-run is how the 2026-09-06
escapes stayed invisible.

A DECLARED broker never skips for any reason. Declaring one is a statement
that it exists, so every way it can fail to work is a failure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.integration.customer_path.redpanda_sasl_harness import (
    DECLARED_BOOTSTRAP_ENV,
    RedpandaSasl,
    declared_bootstrap,
    docker_available,
    resolve_broker,
    stop_redpanda,
)


@pytest.fixture(scope="session")
def redpanda_sasl() -> Iterator[RedpandaSasl]:
    """The one auth-required broker every customer-path test shares.

    Session-scoped on purpose: every module in this package binds to this
    fixture rather than starting a broker of its own, so a run creates at most
    one container -- and, with a broker declared, none at all.
    """
    declared = declared_bootstrap()
    if not declared and not docker_available():
        if os.environ.get("OMN18012_REQUIRE_HARNESS") == "1":
            pytest.fail(
                "OMN18012_REQUIRE_HARNESS=1, no docker daemon is reachable, and "
                f"no broker is declared in {DECLARED_BOOTSTRAP_ENV}; the "
                "customer-path boundary tests did not run"
            )
        pytest.skip(
            "no docker daemon and no declared broker (local developer skip); "
            f"set {DECLARED_BOOTSTRAP_ENV} to run against an existing broker"
        )
    broker = resolve_broker()
    try:
        yield broker
    finally:
        # A no-op for an adopted broker: this harness never tears down a lane
        # it was handed.
        stop_redpanda(broker)


@pytest.fixture
def kafka_auth_env(
    redpanda_sasl: RedpandaSasl, monkeypatch: pytest.MonkeyPatch
) -> RedpandaSasl:
    """Point the process's KAFKA_* env at the auth-required broker."""
    for key, value in redpanda_sasl.env().items():
        monkeypatch.setenv(key, value)
    return redpanda_sasl
