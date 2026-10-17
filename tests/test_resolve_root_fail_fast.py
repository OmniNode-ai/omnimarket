# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for _resolve_root behavior in integration_sweep and dod_sweep handlers.

Both handlers resolve their contracts/artifact root from an explicit configured
root or from ONEX_CC_REPO_PATH, and nothing else. They used to fall back to a
change-control clone located under ``$OMNI_HOME`` when ONEX_CC_REPO_PATH was
stale; that fallback is gone, so neither handler locates a change-control
repository on its own (OMN-17427). A stale or container-only ONEX_CC_REPO_PATH
now fails fast (CLAUDE.md rule #8) whatever ``$OMNI_HOME`` holds.

Covers, for BOTH handlers:
  - ONEX_CC_REPO_PATH unset + no explicit root → RuntimeError
  - ONEX_CC_REPO_PATH set to valid root (exists + contracts/) → returns it
  - Explicit configured root always resolves without touching env
  - ONEX_CC_REPO_PATH invalid + OMNI_HOME holds a registry clone → RuntimeError
  - ONEX_CC_REPO_PATH invalid + OMNI_HOME lacks a registry clone → RuntimeError
  - ONEX_CC_REPO_PATH invalid + OMNI_HOME unset → RuntimeError
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

# The two handlers duplicate an identical ``_resolve_root`` staticmethod; the
# fallback/fail-fast contract must hold for both, so every case is parametrized
# across both callables.
_HANDLERS = [
    pytest.param(
        HandlerIntegrationSweepOrchestrator._resolve_root, id="integration_sweep"
    ),
    pytest.param(HandlerDodSweepOrchestrator._resolve_root, id="dod_sweep"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _without_cc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ONEX_CC_REPO_PATH from the environment for the test."""
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)


# ---------------------------------------------------------------------------
# Unchanged behavior (both handlers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_unset_env_no_configured_root_raises(
    resolve_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_cc_env(monkeypatch)
    with pytest.raises(RuntimeError, match="ONEX_CC_REPO_PATH is not set"):
        resolve_root("")


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_env_set_to_valid_root_resolves(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "contracts").mkdir()
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
    assert resolve_root("") == tmp_path.resolve()


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_explicit_configured_root_bypasses_env(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A configured path always wins and never consults env/OMNI_HOME.
    _without_cc_env(monkeypatch)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert resolve_root(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_explicit_configured_root_overrides_env(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(other))
    assert resolve_root(str(tmp_path)) == tmp_path.resolve()


# ---------------------------------------------------------------------------
# No OMNI_HOME registry fallback: a stale ONEX_CC_REPO_PATH fails fast
# ---------------------------------------------------------------------------


def _make_registry(root: Path) -> Path:
    """Build an OMNI_HOME-style registry holding the change-control clone."""
    cc = root / "onex_change_control"
    (cc / "contracts").mkdir(parents=True)
    return cc


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_env_nonexistent_does_not_fall_back_to_omni_home_registry(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ONEX_CC_REPO_PATH points at a path that does not exist (stale value), and
    # OMNI_HOME holds a registry clone with contracts/: the handler must not
    # locate that clone on its own.
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path / "does_not_exist"))
    _make_registry(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
        resolve_root("")


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_container_path_leak_does_not_fall_back_to_omni_home_registry(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The container mount path exists on the host but has no contracts/ dir.
    leaked = tmp_path / "container_mount"
    leaked.mkdir()
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(leaked))
    _make_registry(tmp_path)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
        resolve_root("")


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_env_invalid_and_omni_home_unset_raises(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Env path lacks contracts/ AND OMNI_HOME is unset: still a RuntimeError,
    # and OMNI_HOME is never read.
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(tmp_path))
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
        resolve_root("")


@pytest.mark.parametrize("resolve_root", _HANDLERS)
def test_env_invalid_and_omni_home_lacks_registry_raises(
    resolve_root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Env path lacks contracts/, OMNI_HOME is set but holds no registry clone
    # → RuntimeError (still fails).
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    omni_home = tmp_path / "empty_home"
    omni_home.mkdir()
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(env_dir))
    monkeypatch.setenv("OMNI_HOME", str(omni_home))
    with pytest.raises(RuntimeError, match="does not exist or lacks a contracts/"):
        resolve_root("")
