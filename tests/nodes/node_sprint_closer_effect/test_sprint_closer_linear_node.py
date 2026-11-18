# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every Linear call of the sprint closer is a request to node_linear_ticket_write_effect, run through the node's
declared CLI (``onex node``): no GraphQL, no Linear client and no key read in the closer's own code."""

from __future__ import annotations

import json
import stat
from datetime import date
from pathlib import Path

import pytest

from omnimarket.handlers.scm.handler_scm_local_state import HandlerScmLocalState
from omnimarket.models.scm import (
    EnumScmLocalRead,
    ModelScmLocalReadRequest,
    ModelScmLocalReadResult,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_prs import (
    HandlerSprintPullRequests,
    current_project,
    pull_request_links,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    NODE_ID,
    HandlerLinearTicketNode,
    HandlerOnexNodeCall,
)
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumChangeKind,
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
        if op == "list_sprint_projects":
            return {
                "sprint_projects": [
                    {
                        "id": "previous",
                        "start_date": "2026-11-09",
                        "target_date": "2026-11-15",
                    },
                    {
                        "id": "current",
                        "start_date": "2026-11-16",
                        "target_date": "2026-11-22",
                    },
                ]
            }
        if op == "list_open":
            return {"candidate_ids": ["OMN-2", "OMN-1"]}
        if op == "read":
            return {
                "ticket_id": payload["ticket_id"],
                "title": "t",
                "description_body": "d",
                "ticket_status": "In Review",
                "assignee": "Owner",
                "attachment_urls": ["https://github.com/OmniNode-ai/example/pull/10"],
            }
        return {"status": "ok"}


def test_activities_call_nodes_every_linear_call_is_a_node_request(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    client = HandlerLinearTicketNode(runner, tmp_path)
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
    client = HandlerLinearTicketNode(RecordingRunner(), tmp_path)
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
    out = HandlerOnexNodeCall((str(script),))(
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
        HandlerOnexNodeCall((str(script),))(
            {"operation": "transition", "ticket_id": "OMN-7", "idempotency_key": "k"}
        )


def test_the_closer_carries_no_direct_linear_access() -> None:
    for path in CLOSER_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "api.linear.app" not in text, path
        assert "urllib.request" not in text, path
        assert "LINEAR_API_KEY" not in text, path


def test_dated_projects_and_attachments_use_guarded_linear_node(tmp_path: Path) -> None:
    runner = RecordingRunner()
    linear = HandlerLinearTicketNode(runner, tmp_path)
    projects = linear.sprint_projects()
    assert current_project(projects, date(2026, 11, 16)) == "current"
    assert current_project(projects, date(2026, 11, 22)) == "current"
    with pytest.raises(RuntimeError, match="expected one active dated sprint"):
        current_project(projects, date(2026, 11, 23))
    issue = linear.read_issue("OMN-7", include_attachments=True)
    assert pull_request_links(issue.attachment_urls) == (("omninode-ai/example", 10),)
    assert [payload["operation"] for payload in runner.requests] == [
        "list_sprint_projects",
        "read",
    ]
    assert runner.requests[-1]["include_attachments"] is True


class FakeScm(HandlerScmLocalState):
    def __init__(self, statuses: dict[int, str], paths: dict[int, str]) -> None:
        super().__init__()
        self.statuses = statuses
        self.paths = paths
        self.pr_reads: list[int] = []

    def handle(self, request: ModelScmLocalReadRequest) -> ModelScmLocalReadResult:
        number = request.pr_number
        assert number is not None
        data: dict[str, str | list[dict[str, str]]]
        if request.read is EnumScmLocalRead.PR_VIEW:
            self.pr_reads.append(number)
            data = {"state": self.statuses[number]}
        elif request.read is EnumScmLocalRead.PR_MERGE_COMMIT:
            data = {"merge_commit_sha": "a" * 40}
        else:
            data = {"files": [{"path": self.paths[number]}]}
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=number,
            answered=True,
            source="watcher",
            result=data,
        )


def test_all_pr_states_are_read_even_after_an_open_pr() -> None:
    scm = FakeScm({1: "OPEN", 2: "MERGED"}, {2: "docs/guide.md"})
    prs = HandlerSprintPullRequests(scm)
    read = prs.read((("omninode-ai/example", 1), ("omninode-ai/example", 2)))
    assert scm.pr_reads == [1, 2]
    assert read.merged == ("omninode-ai/example#2",)
    assert read.blockers == ("linked PR omninode-ai/example#1 is open",)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("tests/unit/test_runner.py", EnumChangeKind.NON_RUNTIME),
        ("plans/plan.yaml", EnumChangeKind.NON_RUNTIME),
        ("contracts/OMN-1.yaml", EnumChangeKind.NON_RUNTIME),
        ("scripts/install-psql.sh", EnumChangeKind.RUNTIME),
        ("scripts/install-psql.sh\tdocs/install.md", EnumChangeKind.RUNTIME),
    ],
)
def test_pr_paths_classify_runtime_fail_closed(path: str, kind: EnumChangeKind) -> None:
    prs = HandlerSprintPullRequests(FakeScm({1: "MERGED"}, {1: path}))
    assert prs.read((("omninode-ai/example", 1),)).change_kind is kind
