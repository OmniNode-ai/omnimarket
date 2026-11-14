# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the loop ports built from the node's ports overlay (OMN-24272)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.code_edit_ports_from_overlay import (
    build_code_edit_ports_from_overlay,
    load_code_edit_ports_overlay,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_code_edit_ports_overlay import (
    CODE_EDIT_PORTS_OVERLAY_ID,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    HandlerDelegatedCodeEditOrchestrator,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    _request,
)

pytestmark = pytest.mark.unit

_OVERLAY = (
    "onex_path: /usr/local/bin/onex\n"
    "state_root: /state/code-edit\n"
    "delegate_lane: code_edit\n"
    "delegate_in_process: true\n"
)


def _write_overlay(root: Path, document: str) -> None:
    node_dir = root / CODE_EDIT_PORTS_OVERLAY_ID
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(document, encoding="utf-8")


def test_valid_overlay_builds_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path, _OVERLAY)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_code_edit_ports_overlay()
    assert overlay is not None
    ports = build_code_edit_ports_from_overlay()
    assert ports is not None
    assert ports.state_root == overlay.state_root


def test_absent_overlay_builds_no_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "/nonexistent")
    assert load_code_edit_ports_overlay() is None
    assert build_code_edit_ports_from_overlay() is None


def test_unknown_key_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_overlay(tmp_path, _OVERLAY + "unknown_key: 1\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_code_edit_ports_overlay()
    with pytest.raises(NodeOverlayError):
        build_code_edit_ports_from_overlay()


def test_run_without_ports_or_overlay_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "/nonexistent")
    handler = HandlerDelegatedCodeEditOrchestrator()
    with pytest.raises(RuntimeError, match="ports overlay"):
        handler.run(_request(max_turns=1))


def test_handler_binds_ports_from_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path, _OVERLAY)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_code_edit_ports_overlay()
    assert overlay is not None
    handler = HandlerDelegatedCodeEditOrchestrator()
    assert handler._ports.state_root == overlay.state_root
