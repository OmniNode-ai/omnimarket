# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the code edit loop overlay reader (OMN-23930)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.code_edit_loop_overlay import load_code_edit_loop_overlay
from omnimarket.handlers.node_overlay_reader import NodeOverlayError


def _write_overlay(root: Path, document: str) -> None:
    node_dir = root / "node_delegated_code_edit_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(document, encoding="utf-8")


def test_valid_document_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_overlay(
        tmp_path,
        "max_turns: 10\ncheck_timeout_seconds: 600\nmax_no_change_finishes: 3\n",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_code_edit_loop_overlay()
    assert overlay is not None
    assert overlay.max_turns == 10
    assert overlay.check_timeout_seconds == 600
    assert overlay.max_no_change_finishes == 3


def test_wrong_key_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_overlay(
        tmp_path,
        "max_turns: 10\ncheck_timeout_seconds: 600\nmax_no_change_finishes: 3\nunknown_key: 1\n",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_code_edit_loop_overlay()


def test_absent_overlay_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "/nonexistent")
    assert load_code_edit_loop_overlay() is None
