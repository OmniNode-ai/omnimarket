# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the merged-running reader and the host table node hold the same rows (OMN-23983).

The merged-running count's host slots come from ``SourceLocalFiles.read_hosts``. It takes the rows from the host table
compute (through the loader the entry binds), never from a yaml parse of its own. The control is the old reader, which
parsed the file itself: it lists the excluded host as available and the comparison below turns red for it.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
)
from omnimarket.nodes.node_host_table_compute.models import (
    ModelHostTableRequest,
    ModelHostTableResult,
)
from omnimarket.nodes.node_merged_running_count_effect.sources import (
    source_local_files,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_local_files import (
    ModelSourceLabels,
    SourceLocalFiles,
)
from tests.nodes.node_host_table_compute.table_support import (
    EXCLUDED,
    three_hosts,
    write_table,
)

pytestmark = pytest.mark.unit

Rows = list[tuple[str, int | None, bool]]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def node_result(env: dict[str, str]) -> ModelHostTableResult:
    return HandlerHostTable().handle(
        ModelHostTableRequest(table=resolve_lab_hosts_table(env))
    )


def sources_over(tmp_path: Path, hosts: Callable[[], ModelHostTableResult]):
    labels = ModelSourceLabels.model_validate(
        yaml.safe_load(
            (Path(source_local_files.__file__).parents[1] / "contract.yaml").read_text()
        )["runtime"]["labels"]
    )
    return SourceLocalFiles(
        watcher_state=tmp_path / "state.json",
        ledger=tmp_path / "ledger.md",
        labels=labels,
        hosts=hosts,
        placement_read=tmp_path / "absent-hosts.json",
        now=NOW,
    )


def reader_rows(tmp_path: Path, env: dict[str, str]) -> Rows:
    sources = sources_over(tmp_path, lambda: node_result(env))
    return [(h.host, h.slots, h.available) for h in sources.read_hosts()]


def test_merged_running_hosts_equal_the_node_rows(tmp_path: Path) -> None:
    env = write_table(tmp_path / "hosts.yaml", *three_hosts())
    expected = [(r.name, r.cap, r.available) for r in node_result(env).rows]
    assert reader_rows(tmp_path, env) == expected
    assert [name for name, _, ok in expected if not ok] == [EXCLUDED]
    assert len(expected) == 3


def test_a_table_the_node_refuses_is_not_read_as_empty(tmp_path: Path) -> None:
    """The reader raises on a table the node refuses; it never returns an empty host list."""
    env = write_table(
        tmp_path / "hosts.yaml",
        {"name": "silent-row", "target": "silent-row.lan", "available": False},
    )
    with pytest.raises(ValueError, match="silent-row"):
        reader_rows(tmp_path, env)


def read_hosts_problems(source: str) -> list[str]:
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "read_hosts"
    )
    body = ast.unparse(function)
    problems = []
    if "yaml.safe_load" in body or "['hosts']" in body:
        problems.append("parses the host table itself")
    if "self._hosts()" not in body:
        problems.append("does not read the rows through the host table loader")
    return problems


def test_read_hosts_has_no_parser_of_its_own() -> None:
    assert read_hosts_problems(inspect.getsource(source_local_files)) == []


def test_an_own_copy_reader_is_found() -> None:
    """Failing control: the old reader body, its own yaml parse of the file, is reported."""
    old = """
def read_hosts(self):
    table = yaml.safe_load(self._hosts_table.read_text())["hosts"]
    return table
"""
    assert read_hosts_problems(old) == [
        "parses the host table itself",
        "does not read the rows through the host table loader",
    ]
