# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare the real schema-1 watcher shape; missing rows must turn the CLI red."""

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_report import (
    ModelPrStateParityReport,
)
from omnimarket.nodes.node_projection_pr_state.parity import main
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = pytest.mark.unit


def files(tmp_path: Path, *, closed: bool = False) -> tuple[Path, Path]:
    e = event().model_dump(mode="json")
    facts = {
        **e,
        "number": e["pr_number"],
        "state": "CLOSED" if closed else "OPEN",
        "body": "DO NOT REPORT THIS BODY",
    }
    state = {
        "schema": 1,
        "last_tick": e["observed_at"],
        "prs": {
            f"{e['repo']}#{e['pr_number']}": {
                "facts": facts,
                "ci": {"verdict": "GREEN", "red": [], "pending": [], "runs": []},
                "cls": e["watcher_class"],
                "queued": False,
            }
        },
    }
    source, export = tmp_path / "state.json", tmp_path / "projection.json"
    source.write_text(json.dumps(state))
    export.write_text(json.dumps({"rows": [e]}))
    return source, export


def run(source: Path, export: Path) -> int:
    return main(["--state-file", str(source), "--projection-json", str(export)])


def test_exact_and_missing_row_positive_control(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, export = files(tmp_path)
    assert run(source, export) == 0
    report = ModelPrStateParityReport.model_validate_json(capsys.readouterr().out)
    assert report.exact
    assert report.file_open_prs == 1
    export.write_text('{"rows": []}')
    assert run(source, export) == 1
    report = ModelPrStateParityReport.model_validate_json(capsys.readouterr().out)
    assert report.mismatches[0].kind == "in-file-not-in-projection"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "wrong"),
        ("draft", True),
        ("armed", True),
        ("labels", []),
        ("watcher_class", "red"),
        ("ci_verdict", "NONE"),
    ],
)
def test_each_field_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, value: object
) -> None:
    source, export = files(tmp_path)
    data = json.loads(export.read_text())
    data["rows"][0][field] = value
    export.write_text(json.dumps(data))
    assert run(source, export) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mismatches"][0]["field"] == field
    assert "DO NOT REPORT" not in json.dumps(report)


def test_sorted_labels_null_ci_and_closed_projection_rows(tmp_path: Path) -> None:
    source, export = files(tmp_path)
    state, projection = json.loads(source.read_text()), json.loads(export.read_text())
    next(iter(state["prs"].values()))["ci"] = None
    projection["rows"][0]["ci_verdict"] = "NONE"
    projection["rows"][0]["labels"].reverse()
    projection["rows"].append(
        {**projection["rows"][0], "pr_number": 99, "state": "merged"}
    )
    source.write_text(json.dumps(state))
    export.write_text(json.dumps(projection))
    assert run(source, export) == 0


def test_open_projection_only_and_empty_are_not_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, export = files(tmp_path, closed=True)
    assert run(source, export) == 1
    assert (
        json.loads(capsys.readouterr().out)["mismatches"][0]["kind"]
        == "in-projection-open-not-in-file"
    )
    export.write_text('{"rows": []}')
    assert run(source, export) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": 2, "prs": {}},
        {"schema": 1},
        {"schema": 1, "prs": {"bad-key": {"facts": {}}}},
    ],
)
def test_invalid_watcher_is_error(tmp_path: Path, bad: object) -> None:
    source, export = files(tmp_path)
    source.write_text(json.dumps(bad))
    assert run(source, export) == 2


def test_missing_dsn_and_missing_files_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, export = files(tmp_path)
    monkeypatch.delenv("PR_STATE_PARITY_TEST_DSN", raising=False)
    assert (
        main(["--state-file", str(source), "--dsn-env", "PR_STATE_PARITY_TEST_DSN"])
        == 2
    )
    assert run(tmp_path / "missing", export) == 2


def test_duplicate_export_key_is_error(tmp_path: Path) -> None:
    source, export = files(tmp_path)
    data = json.loads(export.read_text())
    data["rows"].append(data["rows"][0])
    export.write_text(json.dumps(data))
    assert run(source, export) == 2
