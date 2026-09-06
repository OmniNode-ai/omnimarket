# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18012 gate 1 -- every projection publisher must reach an AUTH-REQUIRED broker.

The escape
----------
``credential_publisher._build_event_bus`` and
``generation_publisher._build_event_bus`` constructed
``ModelKafkaEventBusConfig(bootstrap_servers=...)`` and never called
``apply_environment_overrides()``. That model defaults ``security_protocol``
to ``PLAINTEXT`` with no SASL mechanism, so on onex-dev's IAM-only MSK
listener (:9098) the broker closed the connection, ``bus.start()`` raised
``ONEX_CORE_205_SERVICE_UNAVAILABLE``, and EVERY
``POST /v1/tenants/me/inference-credentials`` returned 503 -- with the
customer's key already written to the secret store. Fixed in omnimarket#2350
(``15afc64f``); the defect entered at ``3a4308c9`` (OMN-16316,
credential_publisher) and ``a0ad3c99`` (OMN-13004, generation_publisher).

Why the landed test is not enough
---------------------------------
``tests/test_omn17372_byok_bus_transport_auth.py`` asserts the CONSTRUCTED
CONFIG of two publishers it names by hand. It is structurally unable to fail
for a third publisher added later, and it never opens a socket, so it cannot
tell a config that *looks* authenticated from a client that actually
authenticates.

This module DISCOVERS every ``_build_event_bus`` in ``omnimarket.projection``
by walking the package, and starts each one against a broker that really
refuses an unauthenticated client. A publisher added tomorrow is covered the
day it is added, and a publisher that regresses to PLAINTEXT fails on the
socket, not on a string comparison.

RESIDUAL
--------
Staging and prod are MSK with ``AWS_MSK_IAM`` + ``SASL_SSL``. This harness is
SCRAM-SHA-256 over ``SASL_PLAINTEXT``. What it proves is "this client did not
silently open PLAINTEXT against an auth-required listener" and NOT "this
client speaks IAM". The mechanism differs; the failure mode pinned here -- a
client that never carried the environment's transport auth at all -- does not.

All credentials in this module are synthetic test constants.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.customer_path.redpanda_sasl_harness import RedpandaSasl

pytestmark = [pytest.mark.integration, pytest.mark.kafka, pytest.mark.slow]

BUILDER_NAME = "_build_event_bus"


def discover_event_bus_builders() -> dict[str, Any]:
    """Every module-level ``_build_event_bus`` under ``omnimarket.projection``.

    Discovery, never a hand-written list. The whole point of this gate is that
    the publisher added next month is covered on the day it is added.
    """
    import omnimarket.projection as projection_pkg

    found: dict[str, Any] = {}
    for module_info in pkgutil.iter_modules(projection_pkg.__path__):
        if module_info.ispkg:
            continue
        module = importlib.import_module(
            f"{projection_pkg.__name__}.{module_info.name}"
        )
        builder = getattr(module, BUILDER_NAME, None)
        if builder is None or not callable(builder):
            continue
        found[f"{projection_pkg.__name__}.{module_info.name}"] = builder
    return found


def _call_builder(builder: Any) -> Any:
    """Call a builder whether or not it takes an optional ``settings``."""
    signature = inspect.signature(builder)
    required = [
        param
        for param in signature.parameters.values()
        if param.default is inspect.Parameter.empty
        and param.kind
        in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    ]
    assert not required, (
        f"{builder.__module__}.{builder.__qualname__} grew a required parameter "
        f"{[p.name for p in required]!r}; this gate can no longer construct it, "
        "which means the publisher would go unproven. Give the parameter a "
        "default or teach this test how to supply it -- do not delete the case."
    )
    return builder()


@pytest.fixture
def projection_broker_env(
    redpanda_sasl: RedpandaSasl, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RedpandaSasl]:
    """Point BOTH the omnimarket Settings and the infra config at the broker."""
    for key, value in redpanda_sasl.env().items():
        monkeypatch.setenv(key, value)
    # omnimarket Settings resolves the bootstrap from its own field.
    monkeypatch.setenv("KAFKA_BROKER", redpanda_sasl.bootstrap)
    monkeypatch.setenv("ENABLE_KAFKA", "true")
    return redpanda_sasl


def test_discovery_finds_the_known_publishers() -> None:
    """Positive control for the discovery itself.

    A discovery that silently found NOTHING would make every parametrised
    case below vanish, and an empty parametrisation is a green test run. This
    is the check that a zero result is never reported as a pass.
    """
    builders = discover_event_bus_builders()
    assert builders, (
        "discovered ZERO event-bus builders under omnimarket.projection; the "
        "walk is broken and every case below would silently not run"
    )
    for known in (
        "omnimarket.projection.credential_publisher",
        "omnimarket.projection.generation_publisher",
    ):
        assert known in builders, (
            f"{known}.{BUILDER_NAME} was not discovered; the walk no longer "
            "sees a publisher this gate is known to cover"
        )


@pytest.mark.parametrize(
    "module_name", sorted(discover_event_bus_builders()), ids=lambda name: name
)
@pytest.mark.asyncio
async def test_publisher_reaches_an_auth_required_broker(
    module_name: str, projection_broker_env: RedpandaSasl
) -> None:
    """The bus this publisher builds must actually authenticate and publish.

    Pre-fix, the constructed config is PLAINTEXT with no SASL mechanism and
    ``bus.start()`` cannot bootstrap from a listener that requires SASL --
    which is exactly the 503 a customer saw on every BYOK registration.
    """
    builder = discover_event_bus_builders()[module_name]
    bus = _call_builder(builder)
    try:
        await bus.start()
    except Exception as exc:
        pytest.fail(
            f"{module_name}.{BUILDER_NAME}() built a bus that could NOT reach an "
            f"auth-required broker: {type(exc).__name__}: {exc}. That is escape "
            "1 of 2026-09-06 -- a publisher whose config never carried the "
            "environment's transport auth, so it opened PLAINTEXT against a "
            "listener that refuses it and every registration 503'd."
        )
    else:
        await bus.close()


@pytest.mark.asyncio
async def test_the_broker_really_refuses_an_unauthenticated_client(
    projection_broker_env: RedpandaSasl,
) -> None:
    """Positive control for the harness.

    Without this, a listener that turned out not to enforce anything would
    make every case above pass for the wrong reason.
    """
    broker = projection_broker_env
    unauth = broker.rpk_unauthenticated("cluster", "info")
    assert unauth.returncode != 0, (
        "the harness listener accepted an UNAUTHENTICATED client; every "
        f"assertion in this module would be vacuous. stdout={unauth.stdout!r} "
        f"stderr={unauth.stderr!r}"
    )
    assert "sasl" in f"{unauth.stdout}{unauth.stderr}".lower()
