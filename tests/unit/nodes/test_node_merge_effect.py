"""
Unit tests for node_merge_effect.
"""

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_merge_effect.handlers.handler_merge import HandlerMergeEffect
from omnimarket.nodes.node_merge_effect.models.model_merge_command import (
    ModelMergeCommand,
)


@pytest.mark.asyncio
async def test_handler_initialization():
    """Handler should initialize without error."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="git version 2.0", stderr=""
        )

        handler = HandlerMergeEffect()
        await handler.initialize()
        assert handler is not None


@pytest.mark.asyncio
async def test_handle_nonexistent_repo():
    """Handler should return error for non-existent repository."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="git version 2.0", stderr=""
        )
        handler = HandlerMergeEffect()
        await handler.initialize()

        data = ModelMergeCommand(
            repo_path="/nonexistent/path",
            branch="feature-branch",
            dry_run=True,
        )
        result = await handler.handle(data)

    assert result["merged"] is False
    assert result["requires_llm"] is False
    assert "does not exist" in result["error"]


@pytest.mark.asyncio
async def test_handle_dry_run():
    """Handler should work in dry-run mode."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        handler = HandlerMergeEffect()
        await handler.initialize()

        data = ModelMergeCommand(
            repo_path="/tmp/test-repo",
            branch="feature-branch",
            dry_run=True,
        )
        result = await handler.handle(data)

        assert mock_run.called
        assert "merged" in result
        assert isinstance(result["merged"], bool)
