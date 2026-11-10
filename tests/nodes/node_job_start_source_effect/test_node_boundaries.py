# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_job_start_source_effect keeps to its own boundary (OMN-22897 review finding 8): no module of the
node imports another node's handler package, and the shared text forms have an owner outside any node."""

from __future__ import annotations

import ast

import pytest

from omnimarket.handlers.reading_text import reason_token, stamp_text
from tests.nodes.node_job_start_source_effect.support import NODE_DIR, NOW

pytestmark = pytest.mark.unit

_OWN = "omnimarket.nodes.node_job_start_source_effect"
_NODES = "omnimarket.nodes."


def _foreign_handler_imports() -> list[str]:
    found: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                if module.startswith(_NODES) and not (
                    module == _OWN or module.startswith(_OWN + ".")
                ):
                    _, _, rest = module[len(_NODES) :].partition(".")
                    if rest.split(".")[0] == "handlers":
                        found.append(f"{path.name}: {module}")
    return found


def test_no_module_of_the_node_imports_another_nodes_handler_package() -> None:
    assert _foreign_handler_imports() == []


def test_the_shared_text_forms_keep_their_shape() -> None:
    assert stamp_text(NOW) == "2026-11-07T12:00:00Z"
    assert reason_token("Org Read/Denied!") == "org-read-denied"
    assert reason_token("!!!") == "unspecified"
