# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ratchet split standalone writers away from direct single-pool writes.

Failure modes: a writer stops registering its contract tables, a write slips
back to ``self.db.execute`` (the legacy tenant alias), or the gate itself
stops recognizing that form. Read-only aggregate queries may use the tenant
alias while their views have no contract table declaration yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRITERS = (
    "node_projection_savings/handlers/handler_savings.py",
    "node_projection_delegation/handlers/handler_delegation.py",
    "node_projection_tenant_credentials/handlers/handler_tenant_credentials_projection.py",
)


def _single_pool_writes(source: str) -> list[int]:
    """Find SQL mutations sent through the legacy unqualified adapter."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "execute"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "db"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
        ):
            continue
        if not node.args:
            found.append(node.lineno)
            continue
        sql = node.args[0]
        if isinstance(sql, ast.JoinedStr):
            literal = "".join(
                part.value for part in sql.values if isinstance(part, ast.Constant)
            )
        elif isinstance(sql, ast.Constant) and isinstance(sql.value, str):
            literal = sql.value
        else:
            found.append(node.lineno)
            continue
        literal = literal.lstrip()
        while literal.startswith(("--", "/*")):
            if literal.startswith("--"):
                literal = literal.partition("\n")[2].lstrip()
            else:
                literal = literal.partition("*/")[2].lstrip()
        # The legacy alias is allowed only for plain tenant-view reads. Treat
        # CTEs and unfamiliar statements as writes until routed explicitly.
        if not literal.upper().startswith("SELECT"):
            found.append(node.lineno)
    return found


@pytest.mark.unit
def test_gate_red_control_catches_legacy_write_and_allows_tenant_read() -> None:
    assert _single_pool_writes(
        "async def write(self):\n"
        "    await self.db.execute(f'INSERT INTO {self.table} VALUES (1)')\n"
    ) == [2]
    assert (
        _single_pool_writes(
            "async def read(self):\n"
            "    await self.db.execute(f'SELECT * FROM {self.table}')\n"
        )
        == []
    )
    assert _single_pool_writes(
        "async def write(self):\n"
        "    await self.db.execute(f'-- reason\\n INSERT INTO {self.table} VALUES (1)')\n"
    ) == [2]
    assert _single_pool_writes(
        "async def write(self):\n"
        "    await self.db.execute(f'WITH row AS (SELECT 1) INSERT INTO {self.table} SELECT * FROM row')\n"
    ) == [2]


@pytest.mark.unit
def test_split_standalone_writers_route_mutations_by_binding() -> None:
    for relpath in WRITERS:
        path = ROOT / "src/omnimarket/nodes" / relpath
        source = path.read_text(encoding="utf-8")
        assert "self._standalone_db_tables = tuple(_tables)" in source, relpath
        assert _single_pool_writes(source) == [], relpath
