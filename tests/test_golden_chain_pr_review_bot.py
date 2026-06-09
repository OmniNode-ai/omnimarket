"""Golden chain tests for node_pr_review_bot.

Exercises DiffFetcher with stub HTTP responses (zero network/LLM calls).
OMN-12856: added to satisfy golden-chain coverage gate after live-path changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_diff_fetcher import (
    DiffFetcherConfig,
    HandlerDiffFetcher,
)
from omnimarket.nodes.node_pr_review_bot.models.models import DiffHunk

_STUB_DIFF = (
    "diff --git a/src/omnimarket/foo.py b/src/omnimarket/foo.py\n"
    "--- a/src/omnimarket/foo.py\n"
    "+++ b/src/omnimarket/foo.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    " def foo():\n"
    "     pass\n"
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_diff_fetcher_parses_stub_diff() -> None:
    """HandlerDiffFetcher.fetch() with a stub HTTP response returns parsed DiffHunk list."""
    config = DiffFetcherConfig(github_token="fake-token")
    fetcher = HandlerDiffFetcher(config)

    with patch.object(
        fetcher,
        "_fetch_raw_diff",
        new=AsyncMock(return_value=_STUB_DIFF),
    ):
        hunks = await fetcher.fetch(pr_number=1, repo="OmniNode-ai/omnimarket")

    assert isinstance(hunks, list)
    assert all(isinstance(h, DiffHunk) for h in hunks)
    assert len(hunks) >= 1
    assert any("foo.py" in h.file_path for h in hunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_diff_fetcher_empty_diff_returns_empty_list() -> None:
    """Empty raw diff returns empty hunk list (no crash)."""
    config = DiffFetcherConfig(github_token="fake-token")
    fetcher = HandlerDiffFetcher(config)

    with patch.object(
        fetcher,
        "_fetch_raw_diff",
        new=AsyncMock(return_value=""),
    ):
        hunks = await fetcher.fetch(pr_number=2, repo="OmniNode-ai/omnimarket")

    assert hunks == []
