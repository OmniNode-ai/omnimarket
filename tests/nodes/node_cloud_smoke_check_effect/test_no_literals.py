"""AC3: no gateway host, IP address or key-prefix literal in the OMN-26711 nodes' source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_LITERAL = re.compile(
    r"https?://[A-Za-z0-9]|[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}|onxk_"
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "node",
    ["node_cloud_smoke_check_effect", "node_customer_path_container_test_effect"],
)
def test_node_source_has_no_host_ip_or_key_literal(node: str) -> None:
    root = _NODES / node
    files = sorted(root.rglob("*.py"))
    assert files, f"{node} has no python source"
    hits = [
        f"{p.relative_to(_NODES)}:{n}: {line.strip()}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _LITERAL.search(line)
    ]
    assert not hits, "\n".join(hits)
