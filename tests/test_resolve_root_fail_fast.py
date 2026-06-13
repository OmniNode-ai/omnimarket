# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for _resolve_root fail-fast behavior in integration_sweep and dod_sweep handlers.

Covers:
  - ONEX_CC_REPO_PATH unset + no explicit root → RuntimeError
  - ONEX_CC_REPO_PATH set to nonexistent path → RuntimeError
  - ONEX_CC_REPO_PATH set to path without contracts/ dir → RuntimeError
  - ONEX_CC_REPO_PATH set to valid root (exists + contracts/ present) → returns resolved path
  - Explicit configured root always resolves without touching env
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ONEX_CC_REPO_PATH from the environment for the test."""
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)


# ---------------------------------------------------------------------------
# integration_sweep: _resolve_root
# ---------------------------------------------------------------------------


class TestIntegrationSweepResolveRoot:
    def test_unset_env_no_configured_root_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _without_env(monkeypatch)
        with pytest.raises(RuntimeError, match="ONEX_CC_REPO_PATH is not set"):
            HandlerIntegrationSweepOrchestrator._resolve_root("")

    def test_env_set_to_nonexistent_path_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        nonexistent = tmp_path / "does_not_exist"
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(nonexistent))
        with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
            HandlerIntegrationSweepOrchestrator._resolve_root("")

    def test_env_set_to_path_without_contracts_dir_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
        with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
            HandlerIntegrationSweepOrchestrator._resolve_root("")

    def test_env_set_to_valid_root_resolves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "contracts").mkdir()
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
        result = HandlerIntegrationSweepOrchestrator._resolve_root("")
        assert result == tmp_path.resolve()

    def test_explicit_configured_root_bypasses_env(self, tmp_path: Path) -> None:
        # env is not set; explicit path always wins
        configured = str(tmp_path)
        result = HandlerIntegrationSweepOrchestrator._resolve_root(configured)
        assert result == tmp_path.resolve()

    def test_explicit_configured_root_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(other))
        result = HandlerIntegrationSweepOrchestrator._resolve_root(str(tmp_path))
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# dod_sweep: _resolve_root
# ---------------------------------------------------------------------------


class TestDodSweepResolveRoot:
    def test_unset_env_no_configured_root_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _without_env(monkeypatch)
        with pytest.raises(RuntimeError, match="ONEX_CC_REPO_PATH is not set"):
            HandlerDodSweepOrchestrator._resolve_root("")

    def test_env_set_to_nonexistent_path_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        nonexistent = tmp_path / "does_not_exist"
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(nonexistent))
        with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
            HandlerDodSweepOrchestrator._resolve_root("")

    def test_env_set_to_path_without_contracts_dir_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
        with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
            HandlerDodSweepOrchestrator._resolve_root("")

    def test_env_set_to_valid_root_resolves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "contracts").mkdir()
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
        result = HandlerDodSweepOrchestrator._resolve_root("")
        assert result == tmp_path.resolve()

    def test_explicit_configured_root_bypasses_env(self, tmp_path: Path) -> None:
        # env is not set; explicit path always wins
        configured = str(tmp_path)
        result = HandlerDodSweepOrchestrator._resolve_root(configured)
        assert result == tmp_path.resolve()

    def test_explicit_configured_root_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("ONEX_CC_REPO_PATH", str(other))
        result = HandlerDodSweepOrchestrator._resolve_root(str(tmp_path))
        assert result == tmp_path.resolve()
