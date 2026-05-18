# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI injection-path tests for OMN-10746.

Verifies that HandlerBaselineCompare accepts an injected capture_handler
and uses it instead of constructing HandlerBaselineCapture internally.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelBaselineSnapshot,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
    ModelBaselineCompareRequest,
)

_NOW = datetime.now(UTC)


def _write_baseline(path: Path) -> None:
    snapshot = ModelBaselineSnapshot(
        baseline_id="test-baseline",
        captured_at=_NOW,
        label="test",
        probes={},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


@pytest.mark.unit
async def test_baseline_compare_accepts_injected_capture_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected capture_handler is used; internal HandlerBaselineCapture not constructed."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))

    baseline_file = tmp_path / "test-baseline.json"
    _write_baseline(baseline_file)

    mock_result = MagicMock()
    mock_result.snapshot = ModelBaselineSnapshot(
        baseline_id="mock-current",
        captured_at=_NOW,
        probes={},
    )
    mock_capture_handler = MagicMock()
    mock_capture_handler.handle = AsyncMock(return_value=mock_result)

    handler = HandlerBaselineCompare(capture_handler=mock_capture_handler)
    request = ModelBaselineCompareRequest(
        baseline_id="test-baseline",
        baseline_path=str(baseline_file),
        dry_run=True,
    )
    result = await handler.handle(request)

    assert mock_capture_handler.handle.called, (
        "Injected capture_handler.handle() must be called"
    )
    assert result.error is None


@pytest.mark.unit
async def test_baseline_compare_default_capture_handler_on_no_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without injection and with a pre-captured snapshot, handler works without calling capture."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))

    baseline_file = tmp_path / "test-baseline.json"
    _write_baseline(baseline_file)

    current_snapshot = ModelBaselineSnapshot(
        baseline_id="current",
        captured_at=_NOW,
        probes={},
    )
    handler = HandlerBaselineCompare()
    request = ModelBaselineCompareRequest(
        baseline_id="test-baseline",
        baseline_path=str(baseline_file),
        current_snapshot=current_snapshot,
        dry_run=True,
    )
    result = await handler.handle(request)

    assert result.error is None
