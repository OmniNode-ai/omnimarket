# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overview exposure declares and serves its token directions.

Failure modes: the view computes the two directions but the exposure does not
list them, so the projection API never returns them; the newest migration that
defines the view inserts them among the existing columns, which Postgres
refuses on CREATE OR REPLACE VIEW; or it drops a column the exposure serves.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_NODE = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_projection_savings"
)
_TOPIC = "onex.snapshot.projection.cost.savings-overview.v1"
_DIRECTIONS = ["tokens_input_total", "tokens_output_total"]


def _exposure_columns() -> list[str]:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    found = [
        entry
        for entry in _walk(contract)
        if isinstance(entry, dict) and entry.get("topic") == _TOPIC
    ]
    assert len(found) == 1, f"positive control: one {_TOPIC} exposure"
    return list(found[0]["columns"])


def _walk(node: object) -> list[object]:
    out: list[object] = [node]
    if isinstance(node, dict):
        for value in node.values():
            out.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk(value))
    return out


def _newest_overview_select() -> list[str]:
    migrations = sorted(
        path
        for path in (_NODE / "migrations").glob("*.sql")
        if "VIEW public.projection_cost_savings_overview AS"
        in path.read_text(encoding="utf-8")
    )
    assert migrations, "positive control: a migration defines the overview view"
    sql = migrations[-1].read_text(encoding="utf-8")
    body = sql[sql.rindex("\nSELECT\n") : sql.index("\nFROM totals\n")]
    return re.findall(
        r"(?:AS\s+\"?(\w+)\"?|totals\.(\w+))\s*,?\s*$", body, re.MULTILINE
    )


def test_exposure_lists_both_directions_after_every_existing_column() -> None:
    columns = _exposure_columns()
    for name in _DIRECTIONS:
        assert name in columns, f"{name} is not served by {_TOPIC}"
    assert columns.index("run_count") < columns.index("tokens_input_total")


def test_newest_view_appends_both_directions_last() -> None:
    names = [first or second for first, second in _newest_overview_select()]
    assert names[-3:] == ["run_count", *_DIRECTIONS], names[-4:]
