# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay location order, pointer precedence and refusal of malformed overlays."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnimarket.models.node_overlay.node_overlay_reader import (
    ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    load_node_overlay,
    overlay_hint,
    require_overlay_keys,
)

NODE = "node_example_compute"
POINTER = "EXAMPLE_NODE_OVERLAY"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROOTS_ENV, raising=False)
    monkeypatch.delenv(POINTER, raising=False)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_roots_and_no_pointer_supplies_no_overlay() -> None:
    assert find_node_overlay(NODE, POINTER) is None
    assert load_node_overlay(NODE, POINTER) is None


def test_first_existing_root_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "a" / NODE / "overlay.yaml", "host: first.example.invalid\n")
    _write(tmp_path / "b" / NODE / "overlay.yaml", "host: second.example.invalid\n")
    monkeypatch.setenv(
        ROOTS_ENV,
        os.pathsep.join(
            ["", str(tmp_path / "missing"), str(tmp_path / "a"), str(tmp_path / "b")]
        ),
    )
    assert load_node_overlay(NODE) == {"host": "first.example.invalid"}


def test_pointer_wins_over_roots_and_never_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "root" / NODE / "overlay.yaml", "host: root.example.invalid\n")
    pointer = _write(tmp_path / "pointed.yaml", "host: pointed.example.invalid\n")
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path / "root"))
    monkeypatch.setenv(POINTER, str(pointer))
    assert load_node_overlay(NODE, POINTER) == {"host": "pointed.example.invalid"}
    monkeypatch.setenv(POINTER, str(tmp_path / "absent.yaml"))
    with pytest.raises(NodeOverlayError, match=POINTER):
        load_node_overlay(NODE, POINTER)


def test_empty_pointer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POINTER, "")
    with pytest.raises(NodeOverlayError, match="empty overlay pointer"):
        find_node_overlay(NODE, POINTER)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "a: [unclosed\n"])
def test_malformed_overlay_is_refused_without_echoing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    _write(tmp_path / NODE / "overlay.yaml", text)
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
    with pytest.raises(NodeOverlayError) as caught:
        load_node_overlay(NODE)
    assert "unclosed" not in str(caught.value)
    assert "just a string" not in str(caught.value)


def test_unknown_keys_are_refused() -> None:
    require_overlay_keys(NODE, {"host": "x"}, frozenset({"host"}))
    with pytest.raises(NodeOverlayError, match="hots"):
        require_overlay_keys(NODE, {"hots": "x"}, frozenset({"host"}))


def test_hint_names_the_roots_variable_and_the_node_file() -> None:
    hint = overlay_hint(NODE, POINTER)
    assert ROOTS_ENV in hint
    assert POINTER in hint
    assert f"{NODE}/overlay.yaml" in hint
