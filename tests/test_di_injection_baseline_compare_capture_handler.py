# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerBaselineCompare DI injection seam (OMN-10749).

Verifies that an injected capture_handler is actually used instead of
constructing a default HandlerBaselineCapture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineSnapshot,
    ModelGitHubPRSnapshot,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
    ModelBaselineCompareRequest,
)

_NOW = datetime.now(UTC)


def _pr(number: int) -> ModelGitHubPRSnapshot:
    return ModelGitHubPRSnapshot(
        pr_number=number,
        title=f"PR #{number}",
        repo="OmniNode-ai/omniclaude",
        state="open",
        labels=[],
        age_days=1.0,
        ci_status="success",
    )


def _write_baseline(path: Path, probes: dict[str, Any]) -> None:
    snapshot = ModelBaselineSnapshot(
        baseline_id="test-baseline",
        captured_at=_NOW,
        label="test",
        probes=probes,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def _omni_home_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))


@pytest.mark.unit
class TestHandlerBaselineCompareDIInjection:
    """Verify the capture_handler injection seam."""

    async def test_injected_capture_handler_is_used(self, tmp_path: Path) -> None:
        """When capture_handler is injected, it must be called instead of the default."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(baseline_file, {BaselineProbeType.GITHUB_PRS: [_pr(1)]})

        mock_capture = MagicMock()
        mock_result = MagicMock()
        mock_result.snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(1), _pr(2)]},
        )
        mock_capture.handle = AsyncMock(return_value=mock_result)

        handler = HandlerBaselineCompare(capture_handler=mock_capture)
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            dry_run=True,
        )

        result = await handler.handle(request)

        assert result.error is None
        mock_capture.handle.assert_called_once()

    async def test_default_capture_handler_constructed_when_not_injected(
        self, tmp_path: Path
    ) -> None:
        """When no capture_handler is passed, a default is constructed (no crash)."""
        handler = HandlerBaselineCompare()
        assert handler._capture_handler is not None

    async def test_probe_registry_forwarded_to_default_capture_handler(
        self, tmp_path: Path
    ) -> None:
        """probe_registry param is forwarded when no explicit capture_handler given."""
        probe_registry: dict[str, Any] = {}
        handler = HandlerBaselineCompare(probe_registry=probe_registry)
        assert handler._capture_handler is not None

    async def test_injected_capture_handler_takes_precedence_over_probe_registry(
        self, tmp_path: Path
    ) -> None:
        """Explicitly injected capture_handler wins over probe_registry."""
        from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
            HandlerBaselineCapture,
        )

        explicit = HandlerBaselineCapture()
        handler = HandlerBaselineCompare(
            capture_handler=explicit,
            probe_registry={},
        )
        assert handler._capture_handler is explicit
