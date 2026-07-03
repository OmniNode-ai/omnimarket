# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""D1 re-home guard: EmitClient owner is omnimarket.events.emit_client (OMN-13213).

Canonical migration Phase D1 moves EmitClient / default_socket_path out of the
non-canonical node_emit_daemon (node_type: service) into the shared
omnimarket.events package, so canonical EFFECT nodes no longer import another
node's private package. These tests pin the new owner and prove the cross-node
import has been eliminated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from omnimarket.events.emit_client import EmitClient, default_socket_path


@pytest.mark.unit
class TestEmitClientOwner:
    """The shared omnimarket.events package is the EmitClient owner."""

    def test_owner_exports_emit_client(self) -> None:
        assert EmitClient.__module__ == "omnimarket.events.emit_client"

    def test_owner_reexported_from_events_package(self) -> None:
        from omnimarket.events import EmitClient as PackageEmitClient
        from omnimarket.events import default_socket_path as package_default

        assert PackageEmitClient is EmitClient
        assert package_default is default_socket_path

    def test_default_socket_path_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEX_EMIT_SOCKET_PATH", "/custom/path.sock")
        assert default_socket_path() == "/custom/path.sock"

    def test_default_socket_path_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert default_socket_path() == "/run/user/1000/onex/emit.sock"

    def test_default_socket_path_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert default_socket_path() == "/tmp/onex-emit.sock"


@pytest.mark.unit
class TestEmitClientDependencyElimination:
    """The node-private client module is gone and no canonical node imports it."""

    def test_node_private_client_module_removed(self) -> None:
        assert (
            importlib.util.find_spec("omnimarket.nodes.node_emit_daemon.client") is None
        ), (
            "node_emit_daemon.client must be deleted; owner is omnimarket.events.emit_client"
        )

    def test_cross_cli_originator_imports_shared_owner(self) -> None:
        src = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_cross_cli_originator"
            / "handlers"
            / "handler_cross_cli_originator.py"
        ).read_text()
        assert "node_emit_daemon.client" not in src
        assert "from omnimarket.events.emit_client import EmitClient" in src
