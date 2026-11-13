# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure match of a ledger RULING row to one merge: override=merge, the pull request and the head."""

from __future__ import annotations

import re
from collections.abc import Sequence

from omnimarket.nodes.node_merge_override_alarm_effect.models import ModelLedgerRow

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_CELL_SEPARATOR = " | "


def row_cells(raw_row: str) -> dict[str, str]:
    """``key=value`` cells of a ledger row; the first occurrence of a key wins."""
    cells: dict[str, str] = {}
    for cell in raw_row.split(_CELL_SEPARATOR):
        key, separator, value = cell.strip().partition("=")
        if separator and key and key not in cells:
            cells[key] = value.strip()
    return cells


def _names_pull_request(cell: str, repository: str, pr_number: int) -> bool:
    name = repository.split("/", 1)[-1]
    return cell in (f"{repository}#{pr_number}", f"{name}#{pr_number}")


def match_override(
    rows: Sequence[ModelLedgerRow],
    repository: str,
    pr_number: int,
    head_sha: str,
) -> ModelLedgerRow | None:
    """The earliest RULING row with override=merge naming this pull request AND this head."""
    matches: list[ModelLedgerRow] = []
    for row in rows:
        if row.row_type != "RULING":
            continue
        cells = row_cells(row.raw_row)
        if cells.get("override") != "merge":
            continue
        if not _names_pull_request(cells.get("pr", ""), repository, pr_number):
            continue
        head = cells.get("head", "")
        if not _FULL_SHA.match(head) or head != head_sha:
            continue
        matches.append(row)
    if not matches:
        return None
    return min(matches, key=lambda row: (row.row_ts, row.row_id))


__all__ = ["match_override", "row_cells"]
