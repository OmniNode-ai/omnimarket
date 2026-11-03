# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22198 - the omnibase-spi pin carries the lane host protocols.

The remote lane start nodes take their host seams from omnibase_spi
(``ProtocolLaneHostTransport``, ``ProtocolLaneHostPool`` and
``ProtocolWorkLedgerStore``), so the pinned spi tag has to contain them. The
pinned tag also carries the ``ProtocolSecretStore.close`` signature that every
secret store of this repo implements.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

pytestmark = pytest.mark.unit

LANE_PROTOCOLS = (
    "ProtocolLaneHostTransport",
    "ProtocolLaneHostPool",
    "ProtocolWorkLedgerStore",
)
TRANSPORT_OPERATIONS = (
    "prepare_worktree",
    "start_engine_detached",
    "read_engine_exit",
    "stop_engine",
    "read_log_tail",
)


@pytest.mark.parametrize("name", LANE_PROTOCOLS)
def test_spi_pin_carries_lane_protocol(name: str) -> None:
    module = importlib.import_module("omnibase_spi.protocols.lane")
    protocol = getattr(module, name)
    assert protocol.__module__.startswith("omnibase_spi."), name


def test_spi_pin_lane_host_transport_operations() -> None:
    module = importlib.import_module("omnibase_spi.protocols.lane")
    transport = module.ProtocolLaneHostTransport
    missing = [op for op in TRANSPORT_OPERATIONS if not hasattr(transport, op)]
    assert missing == []


def test_spi_pin_secret_store_close_takes_optional_timeout() -> None:
    from omnibase_spi.protocols.services.protocol_secret_store import (
        ProtocolSecretStore,
    )

    parameter = inspect.signature(ProtocolSecretStore.close).parameters[
        "timeout_seconds"
    ]
    assert parameter.default is None
