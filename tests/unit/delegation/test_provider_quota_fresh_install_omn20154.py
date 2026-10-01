# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fresh install with no overlay resolves a provider quota reader (OMN-20154).

Imports nothing that only the local-store binding introduced, so it runs (and
fails on the binding error) against a tree that lacks that binding.
"""

from pathlib import Path

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)

pytestmark = pytest.mark.unit


def test_a_fresh_install_with_no_overlay_resolves_a_quota_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The customer path: empty HOME, no overlay, default port construction.

    ``tests/conftest.py`` no longer stubs the quota reader, so this runs the real
    resolution. It fails if a fresh install cannot bind a reader, which is what
    broke ``onex delegate`` before the local-store binding.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ONEX_DATABASE_TOPOLOGY_PROFILE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    dispatch = LocalDelegationDispatchPort()
    snapshot = dispatch._quota_snapshot(())

    assert snapshot.readable
    assert dispatch._quota_reader is not None
