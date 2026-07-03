# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Reproducing test for OMN-13716 — shim_audit: no-arg (paths=None) must not
ValidationError.

Before the fix: ``ModelShimScanRequest()`` raised
  pydantic_core.ValidationError: 1 validation error for ModelShimScanRequest
  paths
    Field required [type=missing, input_value={}, input_type=dict]

After the fix: ``paths`` defaults to ``None`` and the handler resolves workspace
repo roots from OMNI_HOME, returning a valid (possibly empty) result.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from omnimarket.nodes.node_shim_scanner.handlers.handler_shim_scanner import (
    HandlerShimScanner,
    _resolve_paths,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_scan_request import (
    ModelShimScanRequest,
)


@pytest.mark.unit
class TestShimScanRequestDefaultPaths:
    """Reproducing tests for OMN-13716 shim_audit no-arg gap."""

    def test_model_instantiates_without_paths(self) -> None:
        """Before fix: ValidationError. After fix: succeeds with paths=None."""
        req = ModelShimScanRequest()
        assert req.paths is None

    def test_model_with_explicit_paths(self) -> None:
        """Explicit paths must still work as before."""
        req = ModelShimScanRequest(paths=["some/path"])
        assert req.paths == ["some/path"]


@pytest.mark.unit
class TestResolvePathsHelper:
    def test_none_with_omni_home_returns_src_dirs(self, tmp_path: Path) -> None:
        """When paths=None and OMNI_HOME is set, returns existing repo/src dirs."""
        # Build a minimal fake omni_home with a few repo/src dirs
        (tmp_path / "omnibase_compat" / "src").mkdir(parents=True)
        (tmp_path / "omnimarket" / "src").mkdir(parents=True)

        with mock.patch.dict(os.environ, {"OMNI_HOME": str(tmp_path)}):
            resolved = _resolve_paths(None)

        # Should include the src dirs that exist
        assert any("omnibase_compat" in p and p.endswith("src") for p in resolved)
        assert any("omnimarket" in p and p.endswith("src") for p in resolved)

    def test_none_without_omni_home_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When paths=None and OMNI_HOME is unset, returns [] without crashing."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        resolved = _resolve_paths(None)
        assert resolved == []

    def test_explicit_empty_list_passed_through(self) -> None:
        resolved = _resolve_paths([])
        assert resolved == []

    def test_explicit_paths_passed_through(self) -> None:
        resolved = _resolve_paths(["a/path", "b/path"])
        assert resolved == ["a/path", "b/path"]


@pytest.mark.unit
class TestHandlerShimScannerNoArg:
    """Full handler integration for the no-arg invocation path."""

    def test_handle_with_paths_none_and_omni_home_scans_repos(
        self, tmp_path: Path
    ) -> None:
        """Handler invoked with paths=None must complete and return a valid result."""
        import datetime

        # Create a tiny fake repo with one shim-decorated file
        src_dir = tmp_path / "omnibase_compat" / "src"
        src_dir.mkdir(parents=True)
        shim_py = src_dir / "shim_fn.py"
        shim_py.write_text(
            """\
import datetime
from omnibase_core.decorators import shim

@shim(
    ticket_id="OMN-TEST",
    expires_on=datetime.date(2026, 5, 1),
    reason="test shim",
    replacement="NewClass",
)
def old_fn():
    pass
""",
            encoding="utf-8",
        )

        req = ModelShimScanRequest(
            paths=None,
            reference_date=datetime.date(2026, 6, 1).isoformat(),
        )
        with mock.patch.dict(os.environ, {"OMNI_HOME": str(tmp_path)}):
            result = HandlerShimScanner().handle(req)

        # Node ran and returned a valid result object (the shim is expired relative to ref date)
        assert result.total_count >= 1
        assert result.expired_count >= 1

    def test_handle_with_empty_omni_home_returns_empty_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handler with paths=None and no OMNI_HOME must return empty result, not crash."""
        monkeypatch.delenv("OMNI_HOME", raising=False)
        req = ModelShimScanRequest(paths=None)
        result = HandlerShimScanner().handle(req)
        assert result.total_count == 0
        assert result.findings == []
