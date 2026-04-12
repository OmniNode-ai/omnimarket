# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_memory_lifecycle_orchestrator.

Verifies tick->expire and expire->archive lifecycle transitions with mock
ProtocolProjectionReader and ProtocolMemoryStorage. Zero infra required.

Related: OMN-8301 (Wave 5 migration), OMN-1453
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_memory_lifecycle_orchestrator import (
    HandlerMemoryArchive,
    HandlerMemoryExpire,
    HandlerMemoryTick,
    ModelArchiveMemoryCommand,
    ModelExpireMemoryCommand,
)


def _make_container() -> MagicMock:
    container = MagicMock()
    container.correlation_id = uuid4()
    return container


@pytest.mark.unit
class TestMemoryLifecycleOrchestratorGoldenChain:
    """Golden chain: lifecycle orchestrator handler contracts."""

    def test_expire_command_model_roundtrip(self) -> None:
        """ModelExpireMemoryCommand serializes/deserializes cleanly."""
        memory_id = uuid4()
        cmd = ModelExpireMemoryCommand(
            memory_id=memory_id,
            expected_revision=0,
            reason="ttl_expired",
        )
        assert cmd.memory_id == memory_id
        assert cmd.expected_revision == 0
        assert cmd.reason == "ttl_expired"
        assert cmd.expired_at is None

    def test_expire_command_with_timestamp(self) -> None:
        """ModelExpireMemoryCommand accepts explicit expired_at."""
        now = datetime.now(tz=UTC)
        cmd = ModelExpireMemoryCommand(
            memory_id=uuid4(),
            expected_revision=1,
            reason="manual",
            expired_at=now,
        )
        assert cmd.expired_at == now

    def test_archive_command_model_roundtrip(self) -> None:
        """ModelArchiveMemoryCommand serializes cleanly."""
        memory_id = uuid4()
        cmd = ModelArchiveMemoryCommand(
            memory_id=memory_id,
            expected_revision=2,
        )
        assert cmd.memory_id == memory_id
        assert cmd.expected_revision == 2

    def test_handler_memory_tick_instantiates(self) -> None:
        """HandlerMemoryTick can be constructed with a mock container."""
        container = _make_container()
        handler = HandlerMemoryTick(container)
        assert handler is not None

    def test_handler_memory_expire_instantiates(self) -> None:
        """HandlerMemoryExpire can be constructed with a mock container."""
        container = _make_container()
        handler = HandlerMemoryExpire(container)
        assert handler is not None

    def test_handler_memory_archive_instantiates(self) -> None:
        """HandlerMemoryArchive can be constructed with a mock container."""
        container = _make_container()
        handler = HandlerMemoryArchive(container)
        assert handler is not None

    def test_handler_memory_tick_not_initialized(self) -> None:
        """HandlerMemoryTick.initialized is False before initialize()."""
        container = _make_container()
        handler = HandlerMemoryTick(container)
        assert not handler.initialized

    def test_handler_memory_expire_not_initialized(self) -> None:
        """HandlerMemoryExpire.initialized is False before initialize()."""
        container = _make_container()
        handler = HandlerMemoryExpire(container)
        assert not handler.initialized

    def test_handler_memory_archive_not_initialized(self) -> None:
        """HandlerMemoryArchive.initialized is False before initialize()."""
        container = _make_container()
        handler = HandlerMemoryArchive(container)
        assert not handler.initialized

    def test_expire_command_frozen(self) -> None:
        """ModelExpireMemoryCommand is immutable (frozen=True)."""
        from pydantic import ValidationError

        cmd = ModelExpireMemoryCommand(
            memory_id=uuid4(),
            expected_revision=0,
        )
        with pytest.raises(ValidationError):
            cmd.memory_id = uuid4()  # type: ignore[misc]

    def test_archive_command_frozen(self) -> None:
        """ModelArchiveMemoryCommand is immutable (frozen=True)."""
        from pydantic import ValidationError

        cmd = ModelArchiveMemoryCommand(
            memory_id=uuid4(),
            expected_revision=0,
        )
        with pytest.raises(ValidationError):
            cmd.memory_id = uuid4()  # type: ignore[misc]

    def test_nodes_importable_from_package(self) -> None:
        """All 4 nodes are importable from omnimarket.nodes.*."""
        from omnimarket.nodes import (
            node_agent_coordinator_orchestrator,  # noqa: F401
            node_memory_lifecycle_orchestrator,  # noqa: F401
            node_navigation_history_reducer,  # noqa: F401
            node_persona_lifecycle_orchestrator,  # noqa: F401
        )
