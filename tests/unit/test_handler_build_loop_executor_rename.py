# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-8448: HandlerOvernight -> HandlerBuildLoopExecutor rename + alias."""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestHandlerBuildLoopExecutorRename:
    def test_handler_build_loop_executor_importable(self) -> None:
        """HandlerBuildLoopExecutor must be importable from the handler module."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )

        assert HandlerBuildLoopExecutor is not None

    def test_handler_overnight_alias_still_works(self) -> None:
        """HandlerOvernight alias must resolve to the same class as HandlerBuildLoopExecutor."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
            HandlerOvernight,
        )

        assert HandlerOvernight is HandlerBuildLoopExecutor

    def test_handler_build_loop_executor_has_correct_docstring(self) -> None:
        """Class docstring must not reference HandlerOvernight as the primary name."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )

        docstring = HandlerBuildLoopExecutor.__doc__ or ""
        # Must not use "HandlerOvernight" as primary identity in the docstring
        assert "HandlerBuildLoopExecutor" in docstring
        # The old name may appear in a deprecation/alias note, but not as the primary description
        first_line = docstring.strip().split("\n")[0]
        assert "HandlerOvernight" not in first_line
