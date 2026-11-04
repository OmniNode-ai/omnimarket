# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No voter, endpoint, threshold, repository, deadline or host is a literal in the node.

The scan walks the syntax tree of every Python file of the node package and reports a
string constant that names a host, an address, a port, a provider, a model or a repository,
and a numeric constant other than 0 and 1 (a threshold or a deadline). The values come from
the overlay. A planted source proves the scan finds each kind.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator"
)
_STRING = re.compile(
    r"\d{1,3}(?:\.\d{1,3}){3}"
    r"|:\d{4,5}\b"
    r"|\b[\w-]+\.(?:local|lan|internal|com|io|ai)\b"
    r"|anthropic|openai|qwen|glm|gemini|claude|opus|sonnet"
    r"|OmniNode-ai/"
    r"|localhost",
    re.IGNORECASE,
)
_ALLOWED_NUMBERS = {0, 1}
#: Keyword arguments that bound a model field's value. A bound is part of the schema, not a
#: deployment value: the deadline, the thresholds and the quorum size are the field's value.
_SCHEMA_BOUNDS = frozenset({"ge", "gt", "le", "lt", "min_length", "max_length"})


def literal_findings(source: str, name: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    bounds = {
        id(keyword.value)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
        if keyword.arg in _SCHEMA_BOUNDS
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in bounds:
            continue
        if isinstance(node.value, str) and _STRING.search(node.value):
            found.append(f"{name}:{node.lineno} string {node.value!r}")
        elif (
            isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
            and node.value not in _ALLOWED_NUMBERS
        ):
            found.append(f"{name}:{node.lineno} number {node.value!r}")
    return found


def _sources() -> list[Path]:
    return sorted(p for p in _NODE.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_node_package_has_no_deployment_literal() -> None:
    files = _sources()
    assert any(p.name == "handler_agent_review_dispatch.py" for p in files)
    findings = [
        line
        for path in files
        for line in literal_findings(path.read_text("utf-8"), path.name)
    ]
    assert findings == []


@pytest.mark.parametrize(
    "planted",
    [
        'HOST = "192.0.2.5"',
        'ENDPOINT = "gpu.example.local"',
        'PROVIDER = "anthropic"',
        'REPO = "OmniNode-ai/example"',
        "DEADLINE = 1500.0",
        "THRESHOLD = 2",
        "QUORUM = Config(quorum_size=2)",
    ],
)
def test_the_scan_finds_each_kind_of_literal(planted: str) -> None:
    assert literal_findings(planted, "planted.py")


def test_a_schema_bound_on_a_field_is_not_a_deployment_value() -> None:
    assert literal_findings("size: int = Field(ge=2, max_length=63)", "model.py") == []
