# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of the code edit loop overlay reader (OMN-23930)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
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


# ---------------------------------------------------------------------------
# One overlay file, two readers (the deployed shape of the file).
#
# The overlays repository keeps the loop's three limits and the ``event_bus``
# the bus overlay reader needs in the same overlay.yaml. The loop reader once
# refused that file on ``event_bus`` (extra="forbid"), so ``onex code-edit run``
# ended on "overlay ... is invalid" before any turn ran. These tests read one
# document through both readers: the seam fails when either side changes shape.
# ---------------------------------------------------------------------------

_SHARED_DOCUMENT = (
    "max_turns: 20\n"
    "check_timeout_seconds: 600\n"
    "max_no_change_finishes: 2\n"
    "event_bus:\n"
    "  type: kafka\n"
    "  lane: dev\n"
)


def test_shared_file_loads_in_the_loop_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden: the file the bus reader needs is a valid loop overlay."""
    _write_overlay(tmp_path, _SHARED_DOCUMENT)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_code_edit_loop_overlay()
    assert overlay is not None
    assert (overlay.max_turns, overlay.check_timeout_seconds) == (20, 600)
    assert overlay.max_no_change_finishes == 2
    assert overlay.event_bus is not None
    assert (overlay.event_bus.type, overlay.event_bus.lane) == ("kafka", "dev")


def test_shared_file_gives_the_bus_reader_the_same_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam: the loop reader and the bus reader read one document alike."""
    _write_overlay(tmp_path, _SHARED_DOCUMENT)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    overlay = load_code_edit_loop_overlay()
    bus = read_node_event_bus(
        tmp_path / "node_delegated_code_edit_orchestrator" / "overlay.yaml"
    )
    assert overlay is not None
    assert bus is not None
    assert overlay.event_bus == bus


def test_event_bus_only_file_refuses_for_the_missing_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error: the file overlays#507 shipped (event_bus alone) names the three missing keys."""
    _write_overlay(tmp_path, "event_bus:\n  type: kafka\n  lane: dev\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError) as caught:
        load_code_edit_loop_overlay()
    assert str(caught.value).endswith(
        "fields ['check_timeout_seconds', 'max_no_change_finishes', 'max_turns']"
    )


def test_invalid_event_bus_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error: a kafka bus with no lane is refused here as it is by the bus reader."""
    _write_overlay(
        tmp_path,
        "max_turns: 20\ncheck_timeout_seconds: 600\nmax_no_change_finishes: 2\n"
        "event_bus:\n  type: kafka\n",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError) as caught:
        load_code_edit_loop_overlay()
    assert "event_bus" in str(caught.value)
