# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for seam boundary models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ticket_classify_compute.models.model_seam_boundaries import (
    ModelConsumedProtocol,
    ModelSeamBoundaries,
)


class TestModelConsumedProtocol:
    def test_valid_consumed_protocol(self) -> None:
        proto = ModelConsumedProtocol(
            protocol="ProtocolEventBus",
            module="omnibase_spi.protocols.protocol_event_bus",
            mock_available=True,
        )
        assert proto.protocol == "ProtocolEventBus"
        assert proto.mock_available is True

    def test_mock_available_defaults_false(self) -> None:
        proto = ModelConsumedProtocol(
            protocol="ProtocolEventBus",
            module="omnibase_spi.protocols.protocol_event_bus",
        )
        assert proto.mock_available is False

    def test_frozen(self) -> None:
        proto = ModelConsumedProtocol(
            protocol="ProtocolEventBus",
            module="omnibase_spi.protocols.protocol_event_bus",
        )
        with pytest.raises(ValidationError):
            proto.protocol = "other"  # type: ignore[misc]


class TestModelSeamBoundaries:
    def test_empty_seam_boundaries(self) -> None:
        sb = ModelSeamBoundaries()
        assert sb.consumes == ()
        assert sb.produces == ()
        assert sb.topics.subscribe == ()
        assert sb.topics.publish == ()
        assert sb.tables_read == ()
        assert sb.tables_write == ()

    def test_all_mockable_consumes(self) -> None:
        sb = ModelSeamBoundaries(
            consumes=(
                ModelConsumedProtocol(
                    protocol="ProtocolEventBus",
                    module="omnibase_spi.protocols.protocol_event_bus",
                    mock_available=True,
                ),
            ),
        )
        assert sb.all_consumes_mockable is True

    def test_not_all_mockable(self) -> None:
        sb = ModelSeamBoundaries(
            consumes=(
                ModelConsumedProtocol(
                    protocol="ProtocolEventBus",
                    module="omnibase_spi.protocols.protocol_event_bus",
                    mock_available=False,
                ),
            ),
        )
        assert sb.all_consumes_mockable is False

    def test_empty_consumes_is_mockable(self) -> None:
        sb = ModelSeamBoundaries()
        assert sb.all_consumes_mockable is True
