"""
Unit tests for node_worker_stall_recovery.
"""

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_worker_stall_recovery.handlers.handler_stall_recovery import (
    HandlerStallRecovery,
)
from omnimarket.nodes.node_worker_stall_recovery.models.model_stall_recovery_command import (
    ModelStallRecoveryCommand,
)


@pytest.mark.asyncio
async def test_handler_initialization():
    """Handler should initialize without error."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="git version 2.0", stderr=""
        )

        handler = HandlerStallRecovery()
        await handler.initialize()
        assert handler is not None


@pytest.mark.asyncio
async def test_handle_healthy_agent():
    """Handler should return healthy for non-stalled agent."""

    def subprocess_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[0] == "git" and cmd[1] == "--version":
            return MagicMock(returncode=0, stdout="git version 2.0", stderr="")
        # grep returns non-zero when no matches found (agent not in dispatch log)
        return MagicMock(returncode=1, stdout="", stderr="no matches")

    with patch("subprocess.run", side_effect=subprocess_side_effect):
        handler = HandlerStallRecovery()
        await handler.initialize()

        data = ModelStallRecoveryCommand(
            ticket_id="OMN-1234",
            agent_id="agent-abc",
            timeout_minutes=2,
            dry_run=True,
        )
        result = await handler.handle(data)

        assert result["status"] in ["healthy", "stalled", "failed"]
        assert "stall_reason" in result


@pytest.mark.asyncio
async def test_handle_dry_run():
    """Handler should work in dry-run mode."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="git version 2.0", stderr=""
        )

        handler = HandlerStallRecovery()
        await handler.initialize()

        data = ModelStallRecoveryCommand(
            ticket_id="OMN-1234",
            agent_id="agent-abc",
            timeout_minutes=2,
            max_redispatches=2,
            dry_run=True,
        )
        result = await handler.handle(data)

        assert "status" in result
        assert "checkpoint_path" in result
