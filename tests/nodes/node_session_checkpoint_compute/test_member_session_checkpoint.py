# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session checkpoints are bound to the principal that wrote them (two principals, each with one).

A checkpoint records its writer's principal. In a member session ``newest`` selects only that
member's checkpoints, so a newer foreign checkpoint is ignored and nothing it names is resumed; the
PostToolUse recorder binds the principal before it reads any record, bases itself only on the same
member's checkpoints and inherits only from that base; the session lane comes only from the
member's own session.env; the prose-handoff fallback follows the same rule. An operator session
behaves as before.
"""

from __future__ import annotations

import json
import os
import shutil
import time
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

MEMBER_A = [
    {"source": "github_auth", "value": "fixture-member-a-gh"},
    {"source": "broker_binding", "value": "fixture-member-a-dev"},
]
MEMBER_B = [{"source": "github_auth", "value": "fixture-member-b-gh"}]
OPERATOR = [{"source": "github_auth", "value": "fixture-operator-gh"}]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    return {
        "overlay_roots": [str(roots)],
        "directory": str(tmp_path / "handoffs"),
        "sessions": tmp_path / "sessions",
    }


def run(
    env: dict[str, Any], claims: list[dict[str, str]], **fields: Any
) -> ModelSessionCheckpointResult:
    request = ModelSessionCheckpointRequest.model_validate(
        {
            "overlay_roots": env["overlay_roots"],
            "directory": env["directory"],
            "identity_claims": claims,
            **fields,
        }
    )
    return HandlerSessionCheckpoint().handle(request)


def write(
    env: dict[str, Any], claims: list[dict[str, str]], now: str, lane: str, **spec: Any
) -> Path:
    result = run(env, claims, operation="write", now=now, write={"lane": lane, **spec})
    assert result.status == "ok", result
    assert result.path is not None
    return Path(result.path)


def launch(
    session_id: str, task: str, run_id: str, script: str = "/fixture/wf.js"
) -> dict[str, Any]:
    return {
        "payload": {
            "tool_name": "Workflow",
            "session_id": session_id,
            "tool_input": {"scriptPath": script},
            "tool_response": {
                "taskId": task,
                "runId": run_id,
                "scriptPath": script,
                "summary": f"{task} run",
            },
        },
        "hook_mode": "force",
    }


def two_principals(env: dict[str, Any]) -> tuple[Path, Path]:
    """Member A's checkpoint, and a newer one of member B, each naming a running workflow."""
    own = write(
        env,
        MEMBER_A,
        "2026-10-29T08:00:00Z",
        "sess-a",
        session_id="session-aaaaaaaa",
        workflows=(
            "task=task-a;run=run-a;script=/fixture/a.js;purpose=a loop;role=lane;status=running",
        ),
        decisions=("what=a decision;owner=session",),
        notes=("note of a",),
    )
    foreign = write(
        env,
        MEMBER_B,
        "2026-10-29T09:00:00Z",
        "sess-b",
        session_id="session-bbbbbbbb",
        workflows=(
            "task=task-b;run=run-b;script=/fixture/b.js;purpose=b loop;role=lane;status=running",
        ),
        decisions=("what=b decision;owner=operator",),
        deadlines=("what=b deadline;at=2026-10-29T12:00:00Z;action=stop b",),
        ticks=("name=b-tick;cadence=*/30 * * * *;prompt=b tick",),
        notes=("note of b",),
    )
    return own, foreign


def test_checkpoint_records_the_principal_that_wrote_it(env: dict[str, Any]) -> None:
    own, foreign = two_principals(env)
    assert json.loads(own.read_text())["principal"] == "fixture-member-a-dev"
    assert json.loads(foreign.read_text())["principal"] == "fixture-member-b-dev"


# (b) newest in a member session selects only that member's checkpoints.
def test_member_newest_ignores_a_newer_foreign_checkpoint(env: dict[str, Any]) -> None:
    own, _ = two_principals(env)
    unattributed = Path(env["directory"]) / "2026-10-29-100000Z-legacy-checkpoint.json"
    unattributed.write_text(
        json.dumps(
            {
                "schema": "omni.session-checkpoint/1",
                "written_at": "2026-10-29T10:00:00Z",
                "session_lane": "legacy",
                "workflows": [
                    {
                        "task_id": "task-x",
                        "run_id": "run-x",
                        "script": "/x.js",
                        "purpose": "x",
                        "role": "lane",
                        "status": "running",
                    }
                ],
                "decisions": [],
                "deadlines": [],
            }
        )
    )
    result = run(env, MEMBER_A, operation="read")
    assert result.status == "ok"
    assert result.path == str(own)
    text = "\n".join(result.lines)
    assert "RESUME task=task-a run=run-a" in text
    assert "task-b" not in text
    assert "task-x" not in text
    assert "b decision" not in text


def test_member_is_refused_a_foreign_checkpoint_by_path(env: dict[str, Any]) -> None:
    _, foreign = two_principals(env)
    result = run(env, MEMBER_A, operation="read", path=str(foreign))
    assert result.status == "refused"
    assert result.lines == ()


def test_member_with_only_foreign_checkpoints_has_none(env: dict[str, Any]) -> None:
    two_principals(env)
    result = run(
        env,
        [{"source": "environment", "value": "fixture-member-a-dev"}],
        operation="read",
    )
    assert result.scope.privileges == "team"
    assert result.scope.principals == ()
    assert result.status == "absent"
    assert result.lines == ()


# (c) the prose-handoff fallback follows the same rule.
def test_prose_handoff_fallback_is_the_members_own(env: dict[str, Any]) -> None:
    directory = Path(env["directory"])
    directory.mkdir(parents=True)
    own = directory / "2026-10-29-0800-own.md"
    own.write_text("# handoff\nprincipal: fixture-member-a-dev\nresume run-a\n")
    foreign = directory / "2026-10-29-0900-foreign.md"
    foreign.write_text("# handoff\nprincipal: fixture-member-b-dev\nresume run-b\n")
    unattributed = directory / "2026-10-29-1000-unattributed.md"
    unattributed.write_text("# handoff\nresume run-x\n")
    base = time.time()
    for offset, path in enumerate((own, foreign, unattributed)):
        os.utime(path, (base + offset, base + offset))
    member = run(env, MEMBER_A, operation="read")
    assert member.status == "absent"
    assert member.prose_handoff == str(own)
    assert run(env, OPERATOR, operation="read").prose_handoff == str(unattributed)
    assert run(env, [], operation="read").prose_handoff is None


# (b) the hook binds the principal, bases itself on the member's own checkpoints only, and inherits only from that base.
def test_hook_inherits_only_from_the_members_own_base(env: dict[str, Any]) -> None:
    two_principals(env)
    result = run(
        env,
        MEMBER_A,
        operation="record",
        now="2026-10-29T09:30:00Z",
        hook=launch("session-a2a2a2a2", "task-a2", "run-a2"),
    )
    assert result.status == "ok", result
    data = json.loads(Path(str(result.path)).read_text())
    assert data["principal"] == "fixture-member-a-dev"
    assert data["decisions"] == [{"what": "a decision", "owner": "session"}]
    assert data["notes"] == ["note of a"]
    assert data["deadlines"] == []
    assert data["ticks"] == []
    assert [wf["task_id"] for wf in data["workflows"]] == ["task-a2"]


def test_hook_taskstop_never_touches_a_foreign_workflow(env: dict[str, Any]) -> None:
    two_principals(env)
    before = sorted(p.name for p in Path(env["directory"]).iterdir())
    stop = {
        "payload": {
            "tool_name": "TaskStop",
            "session_id": "session-bbbbbbbb",
            "tool_input": {"task_id": "task-b"},
            "tool_response": "stopped",
        },
        "hook_mode": "force",
    }
    result = run(
        env, MEMBER_A, operation="record", now="2026-10-29T09:30:00Z", hook=stop
    )
    assert result.status == "skipped"
    assert sorted(p.name for p in Path(env["directory"]).iterdir()) == before


# (b) a real PostToolUse checkpoint in a member session, then a restart with a newer foreign checkpoint.
def test_restart_after_a_member_hook_resumes_and_inherits_nothing_foreign(
    env: dict[str, Any],
) -> None:
    first = run(
        env,
        MEMBER_A,
        operation="record",
        now="2026-10-29T08:00:00Z",
        hook=launch("session-a1a1a1a1", "task-a1", "run-a1"),
    )
    assert first.status == "ok"
    write(
        env,
        MEMBER_B,
        "2026-10-29T09:00:00Z",
        "sess-b",
        session_id="session-bbbbbbbb",
        workflows=(
            "task=task-b;run=run-b;script=/fixture/b.js;purpose=b loop;role=lane;status=running",
        ),
        decisions=("what=b decision;owner=operator",),
        notes=("note of b",),
    )
    restart = run(env, MEMBER_A, operation="read")
    assert restart.path == first.path
    assert any(
        line.startswith("RESUME task=task-a1 run=run-a1") for line in restart.lines
    )
    assert not any("task-b" in line or "b decision" in line for line in restart.lines)
    second = run(
        env,
        MEMBER_A,
        operation="record",
        now="2026-10-29T09:30:00Z",
        hook=launch("session-a3a3a3a3", "task-a3", "run-a3"),
    )
    data = json.loads(Path(str(second.path)).read_text())
    assert data["decisions"] == []
    assert data["notes"] == []
    assert [wf["task_id"] for wf in data["workflows"]] == ["task-a3"]


def session_env(
    env: dict[str, Any], name: str, lane: str, principal: str | None, age_s: float
) -> None:
    path = env["sessions"] / name / "session.env"
    path.parent.mkdir(parents=True)
    lines = [f"SESSION_LANE={lane}"] + (
        [f"SESSION_PRINCIPAL={principal}"] if principal else []
    )
    path.write_text("\n".join(lines) + "\n")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


# The liveness discovery the hook uses binds a session.env to a principal and refuses an unverified one.
def test_hook_session_lane_comes_only_from_the_members_own_session_env(
    env: dict[str, Any],
) -> None:
    session_env(env, "own", "sess-a", "fixture-member-a-dev", 300)
    session_env(env, "foreign", "sess-b", "fixture-member-b-dev", 60)
    session_env(env, "unverified", "sess-x", None, 30)
    hook = {
        **launch("session-a4a4a4a4", "task-a4", "run-a4"),
        "sessions_dir": str(env["sessions"]),
    }
    member = run(env, MEMBER_A, operation="record", hook=hook)
    assert json.loads(Path(str(member.path)).read_text())["session_lane"] == "sess-a"
    shutil.rmtree(env["sessions"] / "own")
    alone = run(
        env,
        MEMBER_A,
        operation="record",
        hook={**hook, "payload": {**hook["payload"], "session_id": "a5a5a5a5-0000"}},
    )
    assert (
        json.loads(Path(str(alone.path)).read_text())["session_lane"]
        == "session-a5a5a5a5"
    )
    operator = run(
        env,
        OPERATOR,
        operation="record",
        hook={**hook, "payload": {**hook["payload"], "session_id": "session-o1o1o1o1"}},
    )
    assert json.loads(Path(str(operator.path)).read_text())["session_lane"] == "sess-x"


def test_member_cannot_build_on_a_foreign_checkpoint(env: dict[str, Any]) -> None:
    _, foreign = two_principals(env)
    result = run(
        env,
        MEMBER_A,
        operation="write",
        now="2026-10-29T10:00:00Z",
        write={"lane": "sess-a", "from_file": str(foreign)},
    )
    assert result.status == "refused"
    assert result.path is None


# (d) an operator session behaves as before.
def test_operator_session_behaves_as_before(env: dict[str, Any]) -> None:
    _, foreign = two_principals(env)
    read = run(env, OPERATOR, operation="read")
    assert read.scope.privileges == "operator"
    assert read.path == str(foreign)
    result = run(
        env,
        OPERATOR,
        operation="record",
        now="2026-10-29T09:30:00Z",
        hook=launch("session-o2o2o2o2", "task-o", "run-o"),
    )
    data = json.loads(Path(str(result.path)).read_text())
    assert data["principal"] == "fixture-lab-login"
    assert data["notes"] == ["note of b"]
    assert data["ticks"][0]["name"] == "b-tick"


def test_validate_refuses_a_malformed_principal(
    env: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "bad-checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "schema": "omni.session-checkpoint/1",
                "written_at": "2026-10-29T10:00:00Z",
                "session_lane": "s",
                "principal": "two words",
                "workflows": [],
                "decisions": [],
                "deadlines": [],
            }
        )
    )
    result = run(env, OPERATOR, operation="validate", path=str(path))
    assert result.status == "refused"
    assert "principal must be one principal token" in result.findings


def test_hook_gates(env: dict[str, Any]) -> None:
    hook = launch("session-a6a6a6a6", "task-a6", "run-a6")
    assert (
        run(env, MEMBER_A, operation="record", hook={**hook, "hook_mode": "off"}).status
        == "skipped"
    )
    assert (
        run(
            env,
            MEMBER_A,
            operation="record",
            hook={**hook, "hook_mode": None, "entrypoint": "sdk"},
        ).status
        == "skipped"
    )
    assert (
        run(
            env, MEMBER_A, operation="record", hook={**hook, "landing_worker": True}
        ).status
        == "skipped"
    )
