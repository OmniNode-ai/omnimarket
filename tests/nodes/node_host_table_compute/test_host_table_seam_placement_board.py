# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: placement and the board read the same row set of the lab host table (OMN-22251).

One table file (the pin the overlay store's projection writes) is read by the board's host list and by placement. The board
takes its rows from the host table compute; placement judges each host against the same table. The control is a reader with
its own copy of the rows: it lists the excluded host as available, and the comparison below turns red for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_host_table
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
)
from omnimarket.nodes.node_host_table_compute.models import ModelHostTableRequest
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    HandlerLabPlacement,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_placement import (
    ModelLabPlacementRequest,
)
from tests.nodes.node_host_table_compute.table_support import (
    EXCLUDED,
    REASON,
    three_hosts,
    write_table,
)

pytestmark = pytest.mark.unit

Rows = list[tuple[str, int | None, bool, str]]


def node_rows(env: dict[str, str]) -> Rows:
    result = HandlerHostTable().handle(
        ModelHostTableRequest(table=resolve_lab_hosts_table(env))
    )
    return [(r.name, r.cap, r.available, r.exclusion_reason) for r in result.rows]


def board_rows(env: dict[str, str]) -> Rows:
    rows = load_host_table(env)
    assert rows is not None
    return [(r.host, r.cap, r.available, r.unavailable_reason) for r in rows]


def own_copy_rows(path: Path) -> Rows:
    """The reader the board used to have: its own parse of the file, blind to the exclusion's reason and to the schema."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [(r["name"], r.get("cap"), True, "") for r in data["hosts"]]


def differences(left: Rows, right: Rows) -> list[str]:
    """Every host whose row is not the same in both readers' row sets."""
    names = {row[0] for row in left} | {row[0] for row in right}
    by_left, by_right = {r[0]: r for r in left}, {r[0]: r for r in right}
    return sorted(n for n in names if by_left.get(n) != by_right.get(n))


def test_board_and_node_hold_the_same_rows(tmp_path: Path) -> None:
    env = write_table(tmp_path / "hosts.yaml", *three_hosts())
    assert differences(board_rows(env), node_rows(env)) == []
    assert len(board_rows(env)) == len(node_rows(env)) == 3
    assert [name for name, _, ok, _ in board_rows(env) if ok] == [
        "admitted-a",
        "admitted-b",
    ]


def test_placement_refuses_the_row_the_board_lists_as_excluded(tmp_path: Path) -> None:
    env = write_table(tmp_path / "hosts.yaml", *three_hosts())
    table = resolve_lab_hosts_table(env)
    verdicts = (
        HandlerLabPlacement()
        .handle(
            ModelLabPlacementRequest(
                repo="synthetic-repo",
                kind="test",
                host_names=tuple(row.name for row in table.hosts),
                table=table.hosts,
            )
        )
        .by_host()
    )
    excluded = [name for name, _, ok, _ in board_rows(env) if not ok]
    assert excluded == [EXCLUDED]
    assert not verdicts[EXCLUDED].eligible
    assert REASON in verdicts[EXCLUDED].reason
    assert verdicts["admitted-a"].eligible
    assert verdicts["admitted-b"].eligible


def test_a_reader_with_its_own_copy_is_found(tmp_path: Path) -> None:
    """Failing control: the own-copy reader lists the excluded host as available, so the comparison reports it."""
    env = write_table(tmp_path / "hosts.yaml", *three_hosts())
    assert differences(
        own_copy_rows(Path(env["ONEX_LAB_RUN_HOSTS"])), node_rows(env)
    ) == [EXCLUDED]
