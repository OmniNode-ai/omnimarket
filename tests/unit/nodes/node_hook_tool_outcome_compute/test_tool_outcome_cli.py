# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_hook_tool_outcome_compute."""

from __future__ import annotations

import json
import shutil
from io import StringIO
from pathlib import Path

from omnimarket.nodes.node_hook_tool_outcome_compute.runtime_hook_tool_outcome import (
    main,
)

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_tool_outcome_compute"
    / "overlay.example.yaml"
)


def _make_overlay_root(tmp_path: Path) -> Path:
    node_dir = tmp_path / "node_hook_tool_outcome_compute"
    node_dir.mkdir()
    shutil.copy(EXAMPLE, node_dir / "overlay.yaml")
    return tmp_path


def _good_lines() -> list[str]:
    return [
        json.dumps(
            {
                "id": "1",
                "request": {
                    "tool_name": "Bash",
                    "hook_event_name": "PostToolUse",
                    "tool_response": json.dumps(
                        {
                            "stdout": "REFUSED factory-floor-publish: the required step failed",
                            "stderr": "",
                            "interrupted": False,
                        }
                    ),
                    "exit_code": 0,
                },
            }
        ),
        json.dumps(
            {
                "id": "2",
                "request": {
                    "tool_name": "Bash",
                    "hook_event_name": "PostToolUseFailure",
                    "error": "Exit code 1\nls: cannot access x",
                    "exit_code": 1,
                },
            }
        ),
        json.dumps(
            {
                "id": "3",
                "request": {
                    "tool_name": "Bash",
                    "hook_event_name": "PostToolUse",
                    "tool_response": json.dumps(
                        {"stdout": "hello", "stderr": "", "interrupted": False}
                    ),
                    "exit_code": 0,
                },
            }
        ),
    ]


def test_golden_chain(tmp_path: Path) -> None:
    root = _make_overlay_root(tmp_path)
    lines = _good_lines()
    stdin = StringIO("\n".join(lines) + "\n")
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert rc == 0
    out_lines = stdout.getvalue().strip().splitlines()
    assert len(out_lines) == 3
    parsed = [json.loads(line) for line in out_lines]
    assert [p["id"] for p in parsed] == ["1", "2", "3"]
    assert parsed[0]["result"]["outcome"] == "refused"
    assert parsed[0]["result"]["refusing_component"] == "factory-floor-publish"
    assert parsed[1]["result"]["outcome"] == "failed"
    assert parsed[2]["result"]["outcome"] == "ok"


def test_bad_line_not_json(tmp_path: Path) -> None:
    root = _make_overlay_root(tmp_path)
    stdin = StringIO("not json\n")
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert rc == 3
    assert stdout.getvalue() == ""
    err = stderr.getvalue().strip()
    assert err.startswith("INDETERMINATE:")
    assert "1" in err


def test_bad_line_no_id(tmp_path: Path) -> None:
    root = _make_overlay_root(tmp_path)
    stdin = StringIO(
        json.dumps({"request": {"tool_name": "Bash", "hook_event_name": "PostToolUse"}})
        + "\n"
    )
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert rc == 3
    assert stdout.getvalue() == ""
    err = stderr.getvalue().strip()
    assert err.startswith("INDETERMINATE:")
    assert "1" in err


def test_bad_line_pre_tool_use(tmp_path: Path) -> None:
    root = _make_overlay_root(tmp_path)
    stdin = StringIO(
        json.dumps(
            {
                "id": "1",
                "request": {"tool_name": "Bash", "hook_event_name": "PreToolUse"},
            }
        )
        + "\n"
    )
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert rc == 3
    assert stdout.getvalue() == ""
    err = stderr.getvalue().strip()
    assert err.startswith("INDETERMINATE:")
    assert "1" in err


def test_overlay_root_missing(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    stdin = StringIO(
        json.dumps(
            {
                "id": "1",
                "request": {"tool_name": "Bash", "hook_event_name": "PostToolUse"},
            }
        )
        + "\n"
    )
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(empty_root)},
    )
    assert rc == 3
    assert stdout.getvalue() == ""
    err = stderr.getvalue().strip()
    assert err.startswith("INDETERMINATE:")


def test_a_bad_second_line_leaves_no_partial_output(tmp_path: Path) -> None:
    root = _make_overlay_root(tmp_path)
    good = _good_lines()[0]
    bad = json.dumps(
        {"request": {"tool_name": "Bash", "hook_event_name": "PostToolUse"}}
    )
    stdin = StringIO(good + "\n" + bad + "\n")
    stdout = StringIO()
    stderr = StringIO()
    rc = main(
        argv=[],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert rc == 3
    assert stdout.getvalue() == ""
    err = stderr.getvalue().strip()
    assert err.startswith("INDETERMINATE:")
    assert "2" in err
