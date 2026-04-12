# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD-first tests for OMN-8431: omnimarket imports onex_change_control.overseer.

These tests assert the post-migration state. They fail before imports are
updated, pass after.
"""

from __future__ import annotations


class TestOmnimarketOverseerImportMigration:
    def test_handler_overseer_verifier_importable(self) -> None:
        from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
            HandlerOverseerVerifier,
        )

        handler = HandlerOverseerVerifier()
        assert handler is not None

    def test_protocol_overseer_verifier_importable(self) -> None:
        from omnimarket.protocols.protocol_overseer_verifier import (
            ProtocolOverseerVerifier,
        )

        assert ProtocolOverseerVerifier is not None

    def test_no_compat_overseer_in_handler(self) -> None:
        import inspect

        import omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier as mod

        src = inspect.getsource(mod)
        assert "omnibase_compat.overseer" not in src, (
            "handler_overseer_verifier still imports from omnibase_compat.overseer"
        )

    def test_no_compat_overseer_in_protocol(self) -> None:
        import inspect

        import omnimarket.protocols.protocol_overseer_verifier as mod

        src = inspect.getsource(mod)
        assert "omnibase_compat.overseer" not in src, (
            "protocol_overseer_verifier still imports from omnibase_compat.overseer"
        )

    def test_overseer_verify_orchestrator_in_omnimarket(self) -> None:
        from omnimarket.nodes.node_skill_overseer_verify_orchestrator.node import (
            NodeSkillOverseerVerifyOrchestrator,
        )

        assert NodeSkillOverseerVerifyOrchestrator is not None
