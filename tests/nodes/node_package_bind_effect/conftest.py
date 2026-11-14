# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fake omnimarket source tree whose demo node's package ``__init__`` raises, as a heavy node package's does."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

DEMO = "node_demo_bind_compute"
PACKAGE = f"omnimarket.nodes.{DEMO}"


def write_tree(root: Path) -> Path:
    """``root/omnimarket/nodes/<DEMO>`` with a raising ``__init__``, a contract and one handler."""
    pkg = root / "omnimarket" / "nodes" / DEMO
    (pkg / "handlers").mkdir(parents=True)
    (root / "omnimarket" / "__init__.py").write_text("", encoding="utf-8")
    (root / "omnimarket" / "nodes" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text(
        'raise RuntimeError("package init ran")\n', encoding="utf-8"
    )
    (pkg / "contract.yaml").write_text(f"name: {DEMO}\n", encoding="utf-8")
    (pkg / "handlers" / "handler_demo.py").write_text("VALUE = 7\n", encoding="utf-8")
    return root


@pytest.fixture
def demo_tree(tmp_path: Path) -> Iterator[Path]:
    """The fake tree; whatever a bind registered for the demo node is removed afterwards."""
    root = write_tree(tmp_path)
    path_before = list(sys.path)
    yield root
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    nodes = sys.modules.get("omnimarket.nodes")
    if nodes is not None and hasattr(nodes, DEMO):
        delattr(nodes, DEMO)
    sys.path[:] = path_before
