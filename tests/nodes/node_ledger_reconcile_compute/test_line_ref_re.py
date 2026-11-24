# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The line citation pattern of the ledger reconcile compute reads a cited line without naming the file."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_ledger_reconcile_compute.handlers.ledger_rows import (
    LINE_REF_RE,
)

pytestmark = pytest.mark.unit


def _lines(ref: str) -> list[int]:
    return [
        int(match.group(1) or match.group(2)) for match in LINE_REF_RE.finditer(ref)
    ]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("docs/tracking/ROLLING_WORK_LEDGER.md:3508", [3508]),
        ("ROLLING_WORK_LEDGER.md:12 and ledger:45", [12, 45]),
        ("ledger:7", [7]),
        ("LCT1-5-99", [99]),
    ],
)
def test_cited_line_is_read(ref: str, expected: list[int]) -> None:
    assert _lines(ref) == expected


@pytest.mark.parametrize("ref", ["notes.md:12", "README.md:3", "ledgers:9", "line 5"])
def test_other_file_citation_is_not_a_ledger_line(ref: str) -> None:
    assert _lines(ref) == []
