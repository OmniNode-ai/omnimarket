# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board's host list has no parser of its own (OMN-22251): it reads through the one host table reader."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect import source_loader

pytestmark = pytest.mark.unit

READER = "resolve_lab_hosts_table"


def second_parser_problems(source: str) -> list[str]:
    """What is wrong with the source of load_host_table: a yaml parse of its own, or no call of the one reader."""
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "load_host_table"
    )
    body = ast.unparse(function)
    problems = []
    if "yaml.safe_load" in body or "['hosts']" in body:
        problems.append("parses the host table itself")
    if READER not in body:
        problems.append(f"does not call {READER}")
    return problems


def test_load_host_table_reads_through_the_one_reader() -> None:
    assert second_parser_problems(Path(source_loader.__file__).read_text()) == []


def test_an_own_copy_parser_is_found() -> None:
    """Failing control: the old body, with its own yaml parse and no call of the reader, is reported."""
    old = textwrap.dedent(
        """
        def load_host_table(path):
            data = yaml.safe_load(path.read_text())
            return [row["name"] for row in data["hosts"]]
        """
    )
    assert second_parser_problems(old) == [
        "parses the host table itself",
        f"does not call {READER}",
    ]
