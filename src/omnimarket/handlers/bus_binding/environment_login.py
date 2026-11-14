# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker login a container exports, as the writer identity of a bus connection (OMN-24330).

In a container the broker login arrives only as ``KAFKA_SASL_USERNAME`` and ``KAFKA_SASL_PASSWORD``. The lane
credential resolver then answers ``None`` ("the environment path answers"), binds no lane transport, and the
writer identity of a ledger append found no login. The environment login is named only for the life of one
opened connection that bound no transport, and is reset on every exit, a refused broker start included.
A bound lane transport alone names the login: an ambient environment never names the login of a lane that
declares a plaintext or otherwise username-less transport. The password is tested for emptiness and never
read, returned or logged.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from contextvars import ContextVar

_ENV_BROKER_LOGIN: ContextVar[str | None] = ContextVar(
    "onex_lane_bus_environment_broker_login", default=None
)


def environment_broker_login(environ: Mapping[str, str]) -> str | None:
    """The stripped ``KAFKA_SASL_USERNAME`` when it and the password variable are both non-empty, else None."""
    from omnibase_infra.cli.delegate_lane_credentials import (
        ENV_SASL_PASSWORD,
        ENV_SASL_USERNAME,
    )

    username = environ.get(ENV_SASL_USERNAME, "").strip()
    if not username or not environ.get(ENV_SASL_PASSWORD, "").strip():
        return None
    return username


@contextlib.contextmanager
def environment_login_scope(environment_answers: bool) -> Iterator[None]:
    """Name the environment login for one opened connection, only where no lane transport is bound."""
    if not environment_answers:
        yield
        return
    token = _ENV_BROKER_LOGIN.set(environment_broker_login(os.environ))
    try:
        yield
    finally:
        _ENV_BROKER_LOGIN.reset(token)


def named_login() -> str | None:
    """The login the open connection authenticated as, or None when it names none."""
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bound_lane_client_transport,
    )

    transport = bound_lane_client_transport()
    if transport is not None:
        return transport.sasl_username or None
    return _ENV_BROKER_LOGIN.get()
