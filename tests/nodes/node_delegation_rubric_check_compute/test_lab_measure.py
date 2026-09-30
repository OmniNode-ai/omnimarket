# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Counts-only measurement tests built solely from synthetic items."""

import json
from unittest.mock import patch

import pytest

from omnimarket.delegation.rubric.lab_measure import (
    main,
    measure,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_lab_item import (
    ModelLabItem,
)

pytestmark = pytest.mark.unit


def item(
    key,
    label,
    response,
    gate="accepted",
    role="blind_model:claude-opus-5-5",
    task_class="summarization",
):
    return ModelLabItem(
        item_key=key,
        task_class=task_class,
        gate_verdict=gate,
        label=label,
        rater_role=role,
        prompt_snapshot="Summarize SYN-101 with 12000 units.",
        response_snapshot=response,
    )


def test_measure_counts_only():
    rows = [
        item("a", "inadequate", "SYN-999 is done."),
        item("b", "adequate", "SYN-101 is done."),
        item("c", "adequate", "SYN-999 is done."),
        item("d", "adequate", "Things changed."),
        item("e", "inadequate", "SYN-999", gate="refused"),
        item("f", "inadequate", "SYN-999", role="another_rater"),
        item("g", "adequate", "Done", task_class="planning"),
    ]
    report = measure(rows)
    assert report["headline"]["summarization"] == {
        "caught": 1,
        "inadequate_accepted": 1,
        "false_flags": 1,
        "adequate_accepted": 3,
    }
    counts = report["classes"]["summarization"]["counts"]
    assert counts == {"n": 5, "FAIL": 3, "PASS": 1, "UNDETERMINED": 1}
    assert (
        report["classes"]["summarization"]["criteria"]["id_coverage"]["counts"][
            "UNDETERMINED"
        ]
        == 5
    )
    output = json.dumps(report)
    assert "SYN-" not in output
    assert "prompt_snapshot" not in output
    assert "response_snapshot" not in output
    assert "planning" not in report["classes"]


def test_cli_never_prints_validation_content(capsys):
    with patch(
        "omnimarket.delegation.rubric.lab_measure._jsonl_items",
        side_effect=ValueError("SYNTHETIC_PRIVATE_VALUE"),
    ):
        assert main(["--jsonl", "unused.jsonl"]) == 2
    captured = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE_VALUE" not in captured.out + captured.err
    assert not captured.out


def test_jsonl_projects_only_documented_fields():
    import io
    from pathlib import Path

    from omnimarket.delegation.rubric.lab_measure import (
        _jsonl_items,
    )

    row = {
        **item("a", "adequate", "SYN-101").model_dump(),
        "computed_facts": {"synthetic": 1},
    }
    with patch.object(Path, "open", return_value=io.StringIO(json.dumps(row) + "\n")):
        assert list(_jsonl_items(Path("unused.jsonl"))) == [
            item("a", "adequate", "SYN-101")
        ]


def test_measure_filters_explicit_version():
    row = item("a", "inadequate", "SYN-999").model_copy(
        update={"rubric_version": "other-version"}
    )
    assert measure([row])["classes"]["summarization"]["counts"]["n"] == 0


def test_cli_counts_json(capsys):
    with patch(
        "omnimarket.delegation.rubric.lab_measure._jsonl_items",
        return_value=iter([item("a", "adequate", "SYN-101")]),
    ):
        assert main(["--jsonl", "unused.jsonl"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["classes"]["summarization"]["counts"]["PASS"] == 1
