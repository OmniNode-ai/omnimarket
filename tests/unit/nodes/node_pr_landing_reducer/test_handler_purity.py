# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the reducer handler imports no clock, network, bus or database module."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_landing_reducer.handlers import (
    handler_pr_landing_reducer,
)

pytestmark = pytest.mark.unit

_ALLOWED_STDLIB = {"__future__", "collections.abc", "dataclasses", "typing"}
_ALLOWED_PREFIXES = (
    "omnimarket.events.pr_head_check.",
    "omnimarket.events.pr_landing.",
    "omnimarket.events.pr_landing_reduce.",
    "omnimarket.nodes.node_pr_landing_reducer.models.",
)
_FORBIDDEN_NAMES = {
    "now",
    "utcnow",
    "today",
    "time",
    "monotonic",
    "perf_counter",
    "uuid4",
    "random",
}


def _tree() -> ast.Module:
    source = Path(str(handler_pr_landing_reducer.__file__)).read_text(encoding="utf-8")
    return ast.parse(source)


def _imported_modules(tree: ast.Module) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return modules


class TestTheHandlerIsPure:
    def test_it_imports_only_models_and_typing(self) -> None:
        modules = _imported_modules(_tree())
        assert modules, "positive control: the scan must see the imports"
        stray = [
            m
            for m in modules
            if m not in _ALLOWED_STDLIB and not m.startswith(_ALLOWED_PREFIXES)
        ]
        assert not stray, stray

    def test_it_calls_no_clock_or_random_source(self) -> None:
        called = {
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
            for node in ast.walk(_tree())
            if isinstance(node, ast.Call)
        }
        assert "_Move" in called, "positive control: the scan must see the calls"
        assert not called & _FORBIDDEN_NAMES, called & _FORBIDDEN_NAMES

    def test_a_clock_import_would_be_caught(self) -> None:
        tree = ast.parse("from datetime import datetime\nimport socket\n")
        assert set(_imported_modules(tree)) == {"datetime", "socket"}
