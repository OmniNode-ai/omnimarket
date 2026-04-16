# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-8735: HandlerThreadPoster auto-wiring compliance tests.

Verifies that HandlerThreadPoster can be constructed with no arguments
(bridge defaults to None) so the ONEX auto-wiring system can instantiate
it without DI configuration.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_poster import (
    HandlerThreadPoster,
)


@pytest.mark.unit
def test_handler_thread_poster_constructs_with_no_args() -> None:
    """Auto-wiring compliance: handler must be constructable with zero arguments."""
    handler = HandlerThreadPoster()
    assert handler is not None
    assert handler._bridge is None


@pytest.mark.unit
def test_handler_thread_poster_raises_on_post_without_bridge() -> None:
    """Null-guard: calling post() without a bridge raises RuntimeError, not AttributeError."""
    handler = HandlerThreadPoster()
    with pytest.raises(RuntimeError, match="bridge is not configured"):
        handler.post(pr_number=1, repo="owner/repo", findings=(), dry_run=False)
