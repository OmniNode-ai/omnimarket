# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474 AC2: the node source names no key literal."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import omnimarket.nodes.node_tenant_api_key_effect as node_package

pytestmark = pytest.mark.unit

KEY_LITERAL = re.compile(r"plaintext|onxk_")


def test_node_source_names_no_key_literal() -> None:
    root = Path(node_package.__file__).parent
    hits = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".yaml"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if KEY_LITERAL.search(line)
    ]
    assert hits == []
