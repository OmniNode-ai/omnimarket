# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13696: verify handle() stubs on Wave-2 marker handler classes.

Three nodes migrated from omnimemory (OMN-8298 Wave 2) had contracts pointing
handler.class at a marker class with NO handle() method, causing silent dispatch
failure.  This test suite verifies that each class:

  1. Has a callable async handle() method.
  2. Raises NotImplementedError when handle() is called (loud-fail semantics
     while DI/adapter wiring remains in omnimemory).
  3. Satisfies the ProtocolMessageHandler structural protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from omnibase_core.protocols.runtime.protocol_message_handler import (
    ProtocolMessageHandler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_envelope() -> Any:
    """Return a minimal mock envelope (actual envelope model not required here)."""
    return MagicMock()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# NodePersonaRetrievalEffect
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNodePersonaRetrievalEffectHandleStub:
    """handle() is present and raises NotImplementedError."""

    def test_handle_method_exists(self) -> None:
        from omnimarket.nodes.node_persona_retrieval_effect import (
            NodePersonaRetrievalEffect,
        )

        assert callable(getattr(NodePersonaRetrievalEffect, "handle", None)), (
            "NodePersonaRetrievalEffect.handle() is missing"
        )

    def test_handle_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_persona_retrieval_effect import (
            NodePersonaRetrievalEffect,
        )

        node = NodePersonaRetrievalEffect()
        with pytest.raises(NotImplementedError, match="OMN-8298"):
            _run(node.handle(_make_fake_envelope()))

    def test_satisfies_protocol_message_handler(self) -> None:
        from omnimarket.nodes.node_persona_retrieval_effect import (
            NodePersonaRetrievalEffect,
        )

        node = NodePersonaRetrievalEffect()
        assert isinstance(node, ProtocolMessageHandler), (
            "NodePersonaRetrievalEffect does not satisfy ProtocolMessageHandler"
        )

    def test_node_kind_is_effect(self) -> None:
        from omnibase_core.enums import EnumNodeKind

        from omnimarket.nodes.node_persona_retrieval_effect import (
            NodePersonaRetrievalEffect,
        )

        assert NodePersonaRetrievalEffect().node_kind == EnumNodeKind.EFFECT

    def test_handler_id_matches_node_type(self) -> None:
        from omnimarket.nodes.node_persona_retrieval_effect import (
            NodePersonaRetrievalEffect,
        )

        node = NodePersonaRetrievalEffect()
        assert node.handler_id == "node_persona_retrieval_effect"


# ---------------------------------------------------------------------------
# NodePersonaStorageEffect
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNodePersonaStorageEffectHandleStub:
    """handle() is present and raises NotImplementedError."""

    def test_handle_method_exists(self) -> None:
        from omnimarket.nodes.node_persona_storage_effect import (
            NodePersonaStorageEffect,
        )

        assert callable(getattr(NodePersonaStorageEffect, "handle", None)), (
            "NodePersonaStorageEffect.handle() is missing"
        )

    def test_handle_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_persona_storage_effect import (
            NodePersonaStorageEffect,
        )

        node = NodePersonaStorageEffect()
        with pytest.raises(NotImplementedError, match="OMN-8298"):
            _run(node.handle(_make_fake_envelope()))

    def test_satisfies_protocol_message_handler(self) -> None:
        from omnimarket.nodes.node_persona_storage_effect import (
            NodePersonaStorageEffect,
        )

        node = NodePersonaStorageEffect()
        assert isinstance(node, ProtocolMessageHandler), (
            "NodePersonaStorageEffect does not satisfy ProtocolMessageHandler"
        )

    def test_node_kind_is_effect(self) -> None:
        from omnibase_core.enums import EnumNodeKind

        from omnimarket.nodes.node_persona_storage_effect import (
            NodePersonaStorageEffect,
        )

        assert NodePersonaStorageEffect().node_kind == EnumNodeKind.EFFECT

    def test_handler_id_matches_node_type(self) -> None:
        from omnimarket.nodes.node_persona_storage_effect import (
            NodePersonaStorageEffect,
        )

        node = NodePersonaStorageEffect()
        assert node.handler_id == "node_persona_storage_effect"


# ---------------------------------------------------------------------------
# NodeAgentLearningRetrievalEffect
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNodeAgentLearningRetrievalEffectHandleStub:
    """handle() is present and raises NotImplementedError."""

    def test_handle_method_exists(self) -> None:
        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            NodeAgentLearningRetrievalEffect,
        )

        assert callable(getattr(NodeAgentLearningRetrievalEffect, "handle", None)), (
            "NodeAgentLearningRetrievalEffect.handle() is missing"
        )

    def test_handle_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            NodeAgentLearningRetrievalEffect,
        )

        node = NodeAgentLearningRetrievalEffect()
        with pytest.raises(NotImplementedError, match="OMN-8298"):
            _run(node.handle(_make_fake_envelope()))

    def test_satisfies_protocol_message_handler(self) -> None:
        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            NodeAgentLearningRetrievalEffect,
        )

        node = NodeAgentLearningRetrievalEffect()
        assert isinstance(node, ProtocolMessageHandler), (
            "NodeAgentLearningRetrievalEffect does not satisfy ProtocolMessageHandler"
        )

    def test_node_kind_is_effect(self) -> None:
        from omnibase_core.enums import EnumNodeKind

        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            NodeAgentLearningRetrievalEffect,
        )

        assert NodeAgentLearningRetrievalEffect().node_kind == EnumNodeKind.EFFECT

    def test_handler_id_matches_node_type(self) -> None:
        from omnimarket.nodes.node_agent_learning_retrieval_effect import (
            NodeAgentLearningRetrievalEffect,
        )

        node = NodeAgentLearningRetrievalEffect()
        assert node.handler_id == "node_agent_learning_retrieval_effect"
