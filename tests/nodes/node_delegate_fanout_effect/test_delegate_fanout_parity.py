# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20678: old-behaviour oracle captured before removing the JS workflow."""

import json
from pathlib import Path

from omnimarket.nodes.node_delegate_fanout_effect.handlers.handler_delegate_fanout import (
    admit,
    closeout_fragment,
    dispatched_row,
    excerpt,
    markdown_table,
    read_receipt,
    refusal_row,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
)


def test_parity_with_old_workflow() -> None:
    oracle = json.loads(
        (Path(__file__).parent / "fixtures/delegate_fanout_parity.json").read_text()
    )
    expected = oracle["expected"]
    assert [
        admit(ModelFanoutItem.model_validate(x)) for x in oracle["items"]
    ] == expected["admission"]
    rows = [
        dispatched_row(ModelFanoutItem(label="probe"), "dev", x)
        for x in oracle["reads"]
    ]
    assert [r.model_dump() for r in rows] == expected["rows"]
    assert [excerpt(x) for x in oracle["texts"]] == expected["excerpts"]
    assert (
        refusal_row(ModelFanoutItem(label="bad"), "reason", "dev").model_dump()
        == expected["refusal"]
    )
    assert closeout_fragment(rows) == expected["closeout"]
    assert markdown_table(rows) == expected["table"]


def test_receipt_reader_parity_with_original(tmp_path: Path) -> None:

    oracle = json.loads(
        (Path(__file__).parent / "fixtures/delegate_fanout_parity.json").read_text()
    )
    for index, case in enumerate(oracle["reader_cases"]):
        root = tmp_path / str(index)
        root.mkdir()
        if case["receipt"] is not None:
            path = root / "runs/r-1/receipt.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(case["receipt"]))
        for name, key in [
            ("hard_timeout.txt", "marker"),
            ("stdout.txt", "stdout"),
            ("stderr.txt", "stderr"),
        ]:
            if case[key]:
                (root / name).write_text(case[key])
        actual = read_receipt(root)
        if actual["artifacts"]:
            actual["artifacts"] = "<root>/artifacts"
        assert actual == case["expected"]
