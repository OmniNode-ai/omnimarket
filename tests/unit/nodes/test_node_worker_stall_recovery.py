"""
Unit tests for node_worker_stall_recovery.
"""

import json
import tempfile
from datetime import UTC
from pathlib import Path
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
async def test_handle_lookup_failure_returns_healthy_with_reason():
    """Lookup failures (agent not in dispatch log) map to healthy — cannot confirm stall."""

    def subprocess_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
        # grep returns non-zero when no matches found
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

        assert result["status"] == "healthy"
        assert "stall_reason" in result
        assert result["error"] == ""


@pytest.mark.asyncio
async def test_handle_healthy_agent():
    """Handler should return healthy when agent has recent activity in dispatch log."""
    from datetime import datetime

    recent_ts = datetime.now(tz=UTC).isoformat()

    with tempfile.TemporaryDirectory() as tmpdir:
        dispatch_log_dir = Path(tmpdir) / "dispatch-log"
        dispatch_log_dir.mkdir()
        log_file = dispatch_log_dir / "agent-abc.ndjson"
        log_file.write_text(
            json.dumps({"agent_id": "agent-abc", "timestamp": recent_ts}) + "\n"
        )

        def subprocess_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0] == "git" and cmd[1] == "--version":
                return MagicMock(returncode=0, stdout="git version 2.0", stderr="")
            # grep -r -l returns the matching file path
            return MagicMock(returncode=0, stdout=str(log_file) + "\n", stderr="")

        with (
            patch("subprocess.run", side_effect=subprocess_side_effect),
            patch(
                "omnimarket.nodes.node_worker_stall_recovery.handlers.handler_stall_recovery._resolve_onex_state",
                return_value=Path(tmpdir),
            ),
        ):
            handler = HandlerStallRecovery()
            await handler.initialize()

            data = ModelStallRecoveryCommand(
                ticket_id="OMN-1234",
                agent_id="agent-abc",
                timeout_minutes=2,
                dry_run=True,
            )
            result = await handler.handle(data)

            assert result["status"] == "healthy"
            assert result["stall_reason"] == ""


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
