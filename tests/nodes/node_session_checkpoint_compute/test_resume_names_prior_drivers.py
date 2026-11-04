# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A resume that relaunches lanes first still reads the stopped session's driver workflows.

Recorded case: a session stopped while two drain driver workflows kept merges moving. They hold no
CLAIM, so the ledger's dangling-CLAIM listing never names them. The resuming session relaunched its
lanes as workflows; the recorder then wrote a checkpoint for the new session carrying only those
launches, which became the newest, and a read named nothing of the two drivers. A read of the
newest checkpoint now also names the predecessor session's running workflows it does not carry.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_session_checkpoint_compute import HandlerSessionCheckpoint
from omnimarket.nodes.node_session_checkpoint_compute.models import (
    ModelSessionCheckpointRequest,
    ModelSessionCheckpointResult,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
IDENTITY = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"
OPERATOR = [{"source": "github_auth", "value": "fixture-operator-gh"}]
STOPPED = "9f8a0001-stopped"
RESUMER = "aa110002-resumer"


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    return tmp_path


def run(directory: Path, **fields: Any) -> ModelSessionCheckpointResult:
    request = ModelSessionCheckpointRequest.model_validate(
        {
            "directory": str(directory / "handoffs"),
            "overlay_roots": [str(directory / "roots")],
            "identity_claims": OPERATOR,
            **fields,
        }
    )
    return HandlerSessionCheckpoint().handle(request)


def launch(
    directory: Path, now: str, session_id: str, task: str, script: str, name: str = ""
) -> None:
    payload = {
        "tool_name": "Workflow",
        "session_id": session_id,
        "tool_input": {"scriptPath": script},
        "tool_response": {
            "taskId": task,
            "runId": f"run-{task}",
            "scriptPath": script,
            "workflowName": name,
            "summary": f"{task} loop",
        },
    }
    result = run(
        directory,
        operation="record",
        now=now,
        hook={"payload": payload, "hook_mode": "force"},
    )
    assert result.status == "ok", result


def stopped_session(directory: Path) -> None:
    launch(directory, "2026-10-11T17:00:00Z", STOPPED, "drv1", "/wf/merge-drain.js")
    launch(directory, "2026-10-11T17:05:00Z", STOPPED, "drv2", "/wf/repo_drains.js")
    launch(directory, "2026-10-11T18:00:00Z", STOPPED, "lane1", "/wf/lane-a.js")


def test_relaunching_lanes_first_still_names_the_prior_drivers(tmp_path: Path) -> None:
    stopped_session(tmp_path)
    launch(tmp_path, "2026-10-11T19:11:42Z", RESUMER, "lane1b", "/wf/lane-a.js")
    result = run(tmp_path, operation="read")
    assert result.status == "ok"
    assert any(line.startswith("RESUME task=lane1b") for line in result.lines)
    prior = [line for line in result.lines if line.startswith("PRIOR ")]
    assert len(prior) == 2, result.lines
    assert "task=drv1" in prior[0]
    assert "role=drain" in prior[0]
    assert "task=drv2" in prior[1]
    assert "role=drain" in prior[1]
    # The relaunched lane runs the same script, so the stopped lane is not named again.
    assert not any("task=lane1 " in line for line in prior)


def test_a_relaunched_driver_is_not_named_again(tmp_path: Path) -> None:
    stopped_session(tmp_path)
    launch(tmp_path, "2026-10-11T19:11:42Z", RESUMER, "drv1b", "/wf/merge-drain.js")
    lines = run(tmp_path, operation="read").lines
    prior = [line for line in lines if line.startswith("PRIOR ")]
    assert [p.split()[2] for p in prior] == ["task=drv2", "task=lane1"]


def test_a_stopped_or_stale_prior_workflow_is_not_named(tmp_path: Path) -> None:
    stopped_session(tmp_path)
    stop = {
        "tool_name": "TaskStop",
        "session_id": STOPPED,
        "tool_input": {"task_id": "drv2"},
        "tool_response": "stopped",
    }
    assert (
        run(
            tmp_path,
            operation="record",
            now="2026-10-11T18:30:00Z",
            hook={"payload": stop, "hook_mode": "force"},
        ).status
        == "ok"
    )
    launch(tmp_path, "2026-10-11T19:11:42Z", RESUMER, "lane1b", "/wf/lane-a.js")
    prior = [
        ln for ln in run(tmp_path, operation="read").lines if ln.startswith("PRIOR ")
    ]
    assert [p.split()[2] for p in prior] == ["task=drv1"]
    # Past MAX_AGE_HOURS the predecessor is not named at all.
    launch(tmp_path, "2026-10-12T08:00:00Z", "bb220003-later", "x", "/wf/x.js")
    later = run(tmp_path, operation="read").lines
    assert not any(ln.startswith("PRIOR ") and "drv" in ln for ln in later)


def test_the_stopped_sessions_own_read_has_no_prior_lines(tmp_path: Path) -> None:
    stopped_session(tmp_path)
    lines = run(tmp_path, operation="read").lines
    assert not any(line.startswith("PRIOR ") for line in lines)
    assert sum(line.startswith("RESUME ") for line in lines) == 3
