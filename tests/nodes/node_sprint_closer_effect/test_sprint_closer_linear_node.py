# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every Linear call of the sprint closer is a request to node_linear_ticket_write_effect, run through the node's
declared CLI (``onex node``): no GraphQL, no Linear client and no key read in the closer's own code."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    NODE_ID,
    LinearNodeClient,
    OnexNodeRunner,
)

pytestmark = pytest.mark.unit

CLOSER_ROOT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_sprint_closer_effect"
)


class RecordingRunner:
    """Stands in for the node: answers each operation the way the write node's result model does."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        self.requests.append(payload)
        op = payload["operation"]
        if op == "list_open":
            return {"candidate_ids": ["OMN-2", "OMN-1"]}
        if op == "read":
            return {
                "ticket_id": payload["ticket_id"],
                "title": "t",
                "description_body": "d",
                "ticket_status": "In Review",
                "assignee": "Owner",
            }
        return {"status": "ok"}


def test_activities_call_nodes_every_linear_call_is_a_node_request(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    client = LinearNodeClient(runner, tmp_path)
    assert client.open_issue_ids("proj") == ["OMN-1", "OMN-2"]
    issue = client.read_issue("OMN-7")
    assert (issue.state, issue.assignee, issue.closed) == ("In Review", "Owner", False)
    client.comment("OMN-7", "actor: x\nbody")
    client.set_done("OMN-7")
    assert [r["operation"] for r in runner.requests] == [
        "list_open",
        "read",
        "comment",
        "transition",
    ]
    assert runner.requests[0]["project"] == "proj"
    assert runner.requests[3]["state"] == "Done"
    assert all(str(r["idempotency_key"]) for r in runner.requests)


def test_own_last_comment_is_kept_so_the_fingerprint_rule_can_read_it(
    tmp_path: Path,
) -> None:
    client = LinearNodeClient(RecordingRunner(), tmp_path)
    assert client.read_issue("OMN-7").comment_bodies == ()
    client.comment("OMN-7", "actor: x\nclosed by evidence")
    assert client.read_issue("OMN-7").comment_bodies == (
        "actor: x\nclosed by evidence",
    )
    assert client.read_issue("OMN-8").comment_bodies == ()


def _fake_onex(tmp_path: Path, terminal: dict[str, object], rc: int = 0) -> Path:
    """An ``onex`` stand-in: records its argv and writes workflow_result.json under --state-root."""
    script = tmp_path / "onex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "argv = sys.argv[1:]\n"
        "root = pathlib.Path(argv[argv.index('--state-root') + 1])\n"
        "root.mkdir(parents=True)\n"
        "src = pathlib.Path(argv[argv.index('--input') + 1])\n"
        f"(root / 'workflow_result.json').write_text(json.dumps({{'terminal_payload': {json.dumps(terminal)}}}))\n"
        "(pathlib.Path(__file__).with_name('seen.json')).write_text(json.dumps({'argv': argv, 'input': json.loads(src.read_text())}))\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_onex_node_runner_runs_the_declared_cli_and_returns_the_result(
    tmp_path: Path,
) -> None:
    script = _fake_onex(
        tmp_path, {"outcome": "completed", "result": {"ticket_status": "Done"}}
    )
    out = OnexNodeRunner((str(script),))(
        {"operation": "read", "ticket_id": "OMN-7", "idempotency_key": "k"}
    )
    assert out == {"ticket_status": "Done"}
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["argv"][:2] == ["node", NODE_ID]
    assert seen["input"]["requested_by_lane"] == "sprint-closer-workflow"
    assert seen["input"]["operation"] == "read"
    assert seen["input"]["correlation_id"]


def test_a_failed_terminal_raises_with_the_nodes_reason_and_codes(
    tmp_path: Path,
) -> None:
    script = _fake_onex(
        tmp_path,
        {"outcome": "failed", "reason": "guard_refused", "finding_codes": ["x"]},
    )
    with pytest.raises(RuntimeError, match=r"guard_refused.*\['x'\]"):
        OnexNodeRunner((str(script),))(
            {"operation": "transition", "ticket_id": "OMN-7", "idempotency_key": "k"}
        )


def test_the_closer_carries_no_direct_linear_access() -> None:
    for path in CLOSER_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "api.linear.app" not in text, path
        assert "urllib.request" not in text, path
        assert "LINEAR_API_KEY" not in text, path
