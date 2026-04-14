# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for _probe_golden_chain — OMN-8715.

No subprocess, no infra. Patches subprocess.run to control returncode.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    _probe_golden_chain,
)


class TestProbeGoldenChainYellowOnFailure:
    """_probe_golden_chain must return YELLOW (not RED) when subprocess exits non-zero."""

    def _make_completed_process(self, returncode: int, stderr: str = "") -> MagicMock:
        cp = MagicMock(spec=subprocess.CompletedProcess)
        cp.returncode = returncode
        cp.stderr = stderr
        cp.stdout = ""
        return cp

    def test_yellow_on_exit_1(self) -> None:
        """Exit code 1 (sweep found issues / no data) → YELLOW, not RED."""
        with patch(
            "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.subprocess.run",
            return_value=self._make_completed_process(1, "timeout"),
        ):
            result = _probe_golden_chain()

        assert result.status == "YELLOW", (
            f"Expected YELLOW when subprocess exits 1, got {result.status}"
        )
        assert not result.blocks_dispatch, "YELLOW probe must not block dispatch"
        assert result.dimension == "golden_chain"

    def test_yellow_on_timeout_exception(self) -> None:
        """subprocess.TimeoutExpired → YELLOW (no infra available), not RED."""
        with patch(
            "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["uv"], timeout=60),
        ):
            result = _probe_golden_chain()

        assert result.status == "YELLOW", (
            f"Expected YELLOW on TimeoutExpired, got {result.status}"
        )
        assert not result.blocks_dispatch

    def test_green_on_exit_0(self) -> None:
        """Exit code 0 → GREEN (sweep passed)."""
        with patch(
            "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.subprocess.run",
            return_value=self._make_completed_process(0),
        ):
            result = _probe_golden_chain()

        assert result.status == "GREEN"
        assert not result.blocks_dispatch

    def test_yellow_on_file_not_found(self) -> None:
        """FileNotFoundError (uv not on PATH) → YELLOW, not RED."""
        with patch(
            "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.subprocess.run",
            side_effect=FileNotFoundError("uv not found"),
        ):
            result = _probe_golden_chain()

        assert result.status == "YELLOW"
        assert not result.blocks_dispatch
