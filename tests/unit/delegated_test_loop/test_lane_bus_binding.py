# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Principal readback uses only the active transport binding."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)
from omnibase_infra.event_bus.model_lane_client_transport import (
    ModelLaneClientTransport,
)
from pydantic import SecretStr

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, bound_lane_principal


def test_principal_readback_refuses_an_unbound_transport() -> None:
    with pytest.raises(LabRunBusError, match="resolves no broker login"):
        bound_lane_principal()


def test_principal_readback_uses_the_scoped_authenticated_transport() -> None:
    transport = ModelLaneClientTransport(
        lane="test",
        bootstrap_servers="broker.invalid:9092",
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-256",
        sasl_username="test-principal",
        sasl_password=SecretStr("test-credential"),
        declared_in=Path("test.runtime.yaml"),
    )
    with bind_lane_client_transport(transport):
        assert bound_lane_principal() == "test-principal"
    with pytest.raises(LabRunBusError, match="resolves no broker login"):
        bound_lane_principal()


def test_principal_readback_refuses_an_unauthenticated_transport() -> None:
    transport = ModelLaneClientTransport(
        lane="test",
        bootstrap_servers="broker.invalid:9092",
        security_protocol="PLAINTEXT",
        declared_in=Path("test.runtime.yaml"),
    )
    with (
        bind_lane_client_transport(transport),
        pytest.raises(LabRunBusError, match="resolves no broker login"),
    ):
        bound_lane_principal()
