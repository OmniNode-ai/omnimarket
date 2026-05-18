# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the unimported-handler CI gate (OMN-10821)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_unimported_handlers import find_dead_handlers


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.mark.unit
def test_flags_handler_with_no_external_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    dead = find_dead_handlers(tmp_path)
    assert len(dead) == 1
    assert "HandlerXEffect" in dead[0]


@pytest.mark.unit
def test_allows_handler_imported_in_init(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/__init__.py",
        "from .handlers.handler_x import HandlerXEffect\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_allows_handler_referenced_in_registry(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/registry/registry_x.py",
        "from omnimarket.nodes.node_x.handlers.handler_x import HandlerXEffect\n"
        "REGISTRY = {HandlerXEffect}\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_skips_non_handler_classes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class ModelXConfig:\n    pass\n\nclass _InternalHelper:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_respects_inline_allow_annotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "# unimported-handler-allow: abstract base, never directly imported\n"
        "class HandlerXBase:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_skips_test_files(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/test_handler_x.py",
        "class HandlerXMock:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_multiple_handlers_same_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n\nclass HandlerXAlt:\n    pass\n",
    )
    dead = find_dead_handlers(tmp_path)
    names = {entry.split(": ")[-1] for entry in dead}
    assert "HandlerXEffect" in names
    assert "HandlerXAlt" in names


@pytest.mark.unit
def test_partial_import_clears_both_handlers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n\nclass HandlerXAlt:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/__init__.py",
        "from .handlers.handler_x import HandlerXEffect, HandlerXAlt\n",
    )
    assert find_dead_handlers(tmp_path) == []
