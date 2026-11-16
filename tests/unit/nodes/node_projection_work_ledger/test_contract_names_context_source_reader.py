# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection contract names the context-source sequence reader (OMN-24683, AC3).

The projection issues a provisional ledger_seq for a live row. Readers that must treat such a
row as unsequenced are listed in a comment beside ``sequence_assigned_sources``; the
context-source reader is one of them, so a reader of that comment is not left to guess.
"""

from __future__ import annotations

from pathlib import Path

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_work_ledger"
    / "contract.yaml"
)
READER = "node_context_source_ledger_effect"


def test_the_projection_contract_names_the_context_source_sequence_reader() -> None:
    comment_lines = [
        line
        for line in CONTRACT.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#") and READER in line
    ]
    assert comment_lines, (
        f"{CONTRACT} has no comment line naming {READER}; the comment that lists the "
        "readers treating a provisional row as unsequenced must name it"
    )


def test_the_named_reader_is_a_node_that_exists() -> None:
    node_dir = CONTRACT.parents[1] / READER
    assert node_dir.is_dir(), (
        f"{READER} is named in the contract but {node_dir} is absent"
    )
