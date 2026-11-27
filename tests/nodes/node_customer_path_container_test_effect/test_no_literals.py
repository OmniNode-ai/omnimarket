"""AC3: no gateway host, IP address or key-prefix literal in the container test node's source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_customer_path_container_test_effect"
)
_LITERAL = re.compile(
    r"https?://[A-Za-z0-9]|[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}|onxk_"
)


@pytest.mark.unit
def test_container_test_source_has_no_host_ip_or_key_literal() -> None:
    files = sorted(_ROOT.rglob("*.py"))
    assert files, "node_customer_path_container_test_effect has no python source"
    hits = [
        f"{p.relative_to(_ROOT)}:{n}: {line.strip()}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _LITERAL.search(line)
    ]
    assert not hits, "\n".join(hits)
