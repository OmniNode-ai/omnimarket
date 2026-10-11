# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm <verb>`` against a fake bus and a fixture watcher state (OMN-20913, AC1).

The fake bus stands where the runtime does: a command published on a node's
contract command topic is validated as that node's input model and answered
with that node's output model on the contract's terminal topic, at a known
partition and offset. Local-first reads are checked to publish nothing when
the watcher state answers.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.errors import InfraConnectionError

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.handler_scm_dispatch import (
    SCM_REFUSED_VERBS,
    SCM_VERBS,
    contract_operations,
    load_topics,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteMode,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
HEAD = "a" * 40
MOVED = "b" * 40
GATEWAY = load_topics(EnumScmNode.GATEWAY)
WRITE = load_topics(EnumScmNode.WRITE)


@dataclass
class _Message:
    value: bytes
    partition: int
    offset: str


_Callback = Callable[[_Message], Awaitable[None]]


class FakeBus:
    """The runtime's side of the two source-control nodes, in memory."""

    def __init__(self, *, fail_writes: bool = False, answer: bool = True) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._subscribers: dict[str, list[_Callback]] = {}
        self._offset = 41
        self._fail_writes = fail_writes
        self._answer = answer

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        envelope = json.loads(value)
        self.published.append((topic, envelope))
        if not self._answer:
            return
        terminal_topic, payload = self._respond(topic, envelope["payload"])
        self._offset += 1
        reply = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            correlation_id=envelope["correlation_id"],
            parent_envelope_id=envelope["envelope_id"],
            event_type="omnimarket.terminal",
        )
        message = _Message(
            value=json.dumps(reply.model_dump(mode="json")).encode(),
            partition=0,
            offset=str(self._offset),
        )
        for callback in list(self._subscribers.get(terminal_topic, [])):
            await callback(message)

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: _Callback | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        assert on_message is not None
        self._subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self._subscribers[topic].remove(on_message)

        return unsubscribe

    def _respond(
        self, topic: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if topic == GATEWAY.command:
            request = ModelGithubGatewayRequest.model_validate(payload)
            response = ModelGithubGatewayResponse.model_validate(
                {
                    "correlation_id": str(request.correlation_id),
                    "result": _GATEWAY_RESULTS[request.operation.value](request),
                }
            )
            return GATEWAY.success, response.model_dump(mode="json")
        assert topic == WRITE.command, topic
        write = ModelRepoWriteRequest.model_validate(payload)
        common = {
            "correlation_id": write.correlation_id,
            "operation": write.operation,
            "repo": write.repo,
            "mode": EnumRepoWriteMode.ENFORCE,
            "idempotency_key": write.idempotency_key,
            "requested_by_lane": write.requested_by_lane,
            "pr_number": write.pr_number or 77,
        }
        if self._fail_writes:
            failed = ModelRepoWriteFailed(
                **common,
                reason=EnumRepoWriteRefusal.HEAD_MOVED,
                detail="head moved",
            )
            assert WRITE.failure is not None
            return WRITE.failure, failed.model_dump(mode="json")
        completed = ModelRepoWriteCompleted(
            **common, html_url=f"https://github.com/{write.repo}/pull/77", head_sha=HEAD
        )
        return WRITE.success, completed.model_dump(mode="json")


def _pr(request: ModelGithubGatewayRequest) -> dict[str, Any]:
    return {"repo": request.repo, "pr_number": request.pr_number}


_GATEWAY_RESULTS: dict[str, Callable[[ModelGithubGatewayRequest], dict[str, Any]]] = {
    "pr_text": lambda r: {
        "operation": "pr_text",
        **_pr(r),
        "title": "t",
        "body": "b",
        "body_truncated": False,
        "state": "open",
        "is_draft": True,
        "merged": False,
        "author": "a",
        "head_ref": "omn-20913-scratch-1",
        "head_sha": HEAD,
        "base_ref": "dev",
        "truncated": False,
    },
    "pr_status": lambda r: {
        "operation": "pr_status",
        **_pr(r),
        "overall": "green",
        "blocked": False,
        "merge_state_status": "CLEAN",
    },
    "ci_checks": lambda r: {
        "operation": "ci_checks",
        **_pr(r),
        "overall": "green",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "pending": 0,
    },
    "open_prs_list": lambda r: {
        "operation": "open_prs_list",
        "repo": r.repo,
        "count": 0,
    },
    "merge_commit_sha": lambda r: {
        "operation": "merge_commit_sha",
        **_pr(r),
        "merged": False,
    },
    "review_gate": lambda r: {
        "operation": "review_gate",
        **_pr(r),
        "unresolved_threads": 0,
        "blocked": False,
    },
    "ticket_ref": lambda r: {
        "operation": "ticket_ref",
        **_pr(r),
        "head_ref": "jonah/omn-20913-x",
        "ticket_id": "OMN-20913",
    },
    "branch_protection": lambda r: {"operation": "branch_protection", "repo": r.repo},
    "workflow_runs": lambda r: {
        "operation": "workflow_runs",
        "repo": r.repo,
        "truncated": False,
    },
    "run_jobs": lambda r: {
        "operation": "run_jobs",
        "repo": r.repo,
        "run_id": r.run_id,
        "truncated": False,
    },
    "job_log_tail": lambda r: {
        "operation": "job_log_tail",
        "repo": r.repo,
        "job_id": r.job_id,
        "total_bytes": 4,
        "truncated": False,
        "text": "done",
    },
    "artifacts": lambda r: {
        "operation": "artifacts",
        "repo": r.repo,
        "run_id": r.run_id,
        "truncated": False,
    },
    "releases": lambda r: {
        "operation": "releases",
        "repo": r.repo,
        "releases_truncated": False,
        "tags_truncated": False,
    },
    "pr_heads": lambda r: {
        "operation": "pr_heads",
        "repo": r.repo,
        "truncated": False,
    },
}


def _install(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    @contextlib.asynccontextmanager
    async def opened(_: dict[str, object]) -> AsyncIterator[FakeBus]:
        yield bus

    monkeypatch.setattr(scm_cli, "_open_bus", opened)


def _watcher(tmp_path: Path, *, age: timedelta = timedelta(minutes=1)) -> Path:
    tick = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "schema": 1,
        "last_tick": tick,
        "prs": {
            "omnimarket#77": {
                "cls": "pending",
                "reason": "1 pending: CI Summary",
                "blocked": False,
                "handoff": None,
                "ci": {
                    "read_at": tick,
                    "red": [],
                    "pending": ["CI Summary"],
                    "cancelled": [],
                    "red_facts": [],
                    "runs": [["CI Summary", "in_progress", None, tick]],
                    "detail": [["CI Summary", "9001", tick]],
                },
                "facts": {
                    "number": 77,
                    "title": "feat(OMN-20913): x",
                    "state": "OPEN",
                    "draft": False,
                    "armed": False,
                    "author": "a",
                    "base": "dev",
                    "head_ref": "jonah/omn-20913-x",
                    "head_sha": HEAD,
                    "labels": [],
                    "body": "long body",
                },
            },
            "omnimarket#70": {
                "cls": "merged",
                "facts": {"number": 70, "state": "MERGED", "base": "dev"},
            },
        },
        "merges": {
            "omnimarket#70": {"merge_sha": "c" * 40, "base": "dev"},
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _run(args: list[str]) -> Result:
    return CliRunner().invoke(scm_cli.scm_group, args, catch_exceptions=False)


def _answer(result: Result) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return loaded


# -------------------------------------------------------------- the table


def test_the_bus_is_opened_with_the_command_line_bus_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def opener(
        *, bus: str, lane: str | None, kafka_bootstrap: str | None, root: Path | None
    ) -> object:
        seen.update(bus=bus, lane=lane, kafka_bootstrap=kafka_bootstrap, root=root)
        return object()

    monkeypatch.setattr(scm_cli, "open_lab_run_bus", opener)
    scm_cli._open_bus(
        {
            "bus": "kafka",
            "bus_lane": "dev",
            "kafka_bootstrap": None,
            "omnibase_path": Path("/w"),
        }
    )
    assert seen == {
        "bus": "kafka",
        "lane": "dev",
        "kafka_bootstrap": None,
        "root": Path("/w"),
    }


def test_every_contract_operation_has_exactly_one_verb() -> None:
    for node in EnumScmNode:
        declared = sorted(contract_operations(node))
        served = sorted(s.operation for s in SCM_VERBS if s.node is node)
        assert declared, node
        assert served == declared, node


def test_no_refused_verb_is_also_served() -> None:
    served = {s.verb for s in SCM_VERBS}
    assert served.isdisjoint({r.verb for r in SCM_REFUSED_VERBS})
    assert len(served) == len(SCM_VERBS)


# ------------------------------------------------------- bus-answered verbs

_READ_ARGS: dict[str, list[str]] = {
    "pr-view": [f"{REPO}#77", "--source", "bus"],
    "pr-status": [f"{REPO}#77", "--source", "bus"],
    "pr-checks": [f"{REPO}#77", "--source", "bus"],
    "pr-list": [REPO, "--source", "bus"],
    "pr-merge-commit": [f"{REPO}#77", "--source", "bus"],
    "pr-review": [f"{REPO}#77"],
    "pr-ticket": [f"{REPO}#77"],
    "branch-protection": [REPO, "--branch", "dev"],
    "run-list": [REPO, "--branch", "dev", "--limit", "5"],
    "run-view": [REPO, "--run-id", "11"],
    "job-log": [REPO, "--job-id", "12", "--tail-bytes", "1024"],
    "run-download": [REPO, "--run-id", "11"],
    "release-list": [REPO],
    "pr-heads": [REPO, "--source", "bus"],
}
_WRITE_ARGS: dict[str, list[str]] = {
    "pr-create": [
        REPO,
        "--head",
        "omn-20913-scratch-1",
        "--base",
        "dev",
        "--title",
        "scratch",
        "--body",
        "b",
        "--draft",
    ],
    "pr-edit": [f"{REPO}#77", "--title", "t2", "--head-sha", HEAD],
    "pr-ready": [f"{REPO}#77", "--head-sha", HEAD],
    "pr-close": [
        f"{REPO}#77",
        "--head-sha",
        HEAD,
        "--run",
        "run-1",
        "--claims-dir",
        "/tmp/claims",
    ],
    "pr-comment": [f"{REPO}#77", "--body", "hello"],
    "workflow-run": [REPO, "--workflow", "ci.yml", "--ref", "dev", "--input", "a=b"],
}


def test_every_served_verb_is_exercised() -> None:
    assert set(_READ_ARGS) | set(_WRITE_ARGS) == {s.verb for s in SCM_VERBS}


@pytest.mark.parametrize("verb_name", sorted(_READ_ARGS))
def test_read_verb_publishes_typed_command_and_returns_terminal(
    monkeypatch: pytest.MonkeyPatch, verb_name: str
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    spec = next(s for s in SCM_VERBS if s.verb == verb_name)

    result = _run([verb_name, *_READ_ARGS[verb_name]])

    assert result.exit_code == 0, result.output
    assert len(bus.published) == 1
    topic, envelope = bus.published[0]
    assert topic == GATEWAY.command
    sent = ModelGithubGatewayRequest.model_validate(envelope["payload"])
    assert sent.operation.value == spec.operation
    answer = _answer(result)
    assert answer["source"] == "bus"
    assert answer["outcome"] == "completed"
    assert answer["result"]["operation"] == spec.operation
    assert answer["bus"]["terminal_topic"] == GATEWAY.success
    assert answer["bus"]["terminal_offset"] == "42"
    assert answer["bus"]["terminal_partition"] == 0
    assert answer["bus"]["command_envelope_id"] == envelope["envelope_id"]
    assert answer["bus"]["correlation_id"] == str(sent.correlation_id)


@pytest.mark.parametrize("verb_name", sorted(_WRITE_ARGS))
def test_write_verb_publishes_typed_command_and_returns_terminal(
    monkeypatch: pytest.MonkeyPatch, verb_name: str
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    spec = next(s for s in SCM_VERBS if s.verb == verb_name)

    result = _run([verb_name, *_WRITE_ARGS[verb_name], "--lane", "lane-x"])

    assert result.exit_code == 0, result.output
    topic, envelope = bus.published[0]
    assert topic == WRITE.command
    sent = ModelRepoWriteRequest.model_validate(envelope["payload"])
    assert sent.operation.value == spec.operation
    assert sent.requested_by_lane == "lane-x"
    answer = _answer(result)
    assert answer["outcome"] == "completed"
    assert answer["bus"]["terminal_topic"] == WRITE.success
    assert answer["result"]["operation"] == spec.operation


def test_write_idempotency_key_is_stable_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    args = ["pr-comment", *_WRITE_ARGS["pr-comment"], "--lane", "lane-x"]
    _run(args)
    _run(args)
    keys = [envelope["payload"]["idempotency_key"] for _, envelope in bus.published]
    assert keys[0] == keys[1]
    assert (
        bus.published[0][1]["correlation_id"] != bus.published[1][1]["correlation_id"]
    )


def test_failed_write_terminal_exits_one_on_the_failure_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeBus(fail_writes=True))
    result = _run(["pr-ready", *_WRITE_ARGS["pr-ready"], "--lane", "lane-x"])
    assert result.exit_code == scm_cli.EXIT_FAILED
    answer = _answer(result)
    assert answer["outcome"] == "failed"
    assert answer["bus"]["terminal_topic"] == WRITE.failure
    assert answer["result"]["reason"] == "head_moved"


def test_no_terminal_exits_75(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeBus(answer=False))
    result = _run(["release-list", REPO, "--timeout-s", "0.05"])
    assert result.exit_code == scm_cli.EXIT_NO_TERMINAL
    assert json.loads(result.stdout)["status"] == "no_terminal"


def test_a_bus_refusal_exits_69_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RefusingBus(FakeBus):
        async def subscribe(self, topic: str, *args: Any, **kwargs: Any) -> Any:
            raise InfraConnectionError(f"TopicAuthorizationFailedError: {topic}")

    _install(monkeypatch, RefusingBus())
    result = CliRunner().invoke(scm_cli.scm_group, ["release-list", REPO])
    assert result.exit_code == scm_cli.EXIT_BUS_UNAVAILABLE
    assert "TopicAuthorizationFailedError" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_write_expected_head_comes_from_the_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path)
    result = _run(
        ["pr-ready", f"{REPO}#77", "--lane", "lane-x", "--watcher-state", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert bus.published[0][1]["payload"]["expected_head_sha"] == HEAD


def test_write_without_a_head_anywhere_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path)
    result = CliRunner().invoke(
        scm_cli.scm_group,
        ["pr-ready", f"{REPO}#5", "--lane", "lane-x", "--watcher-state", str(state)],
    )
    assert result.exit_code == 2
    assert "--head-sha" in result.output
    assert bus.published == []


# ------------------------------------------------------------- local reads


@pytest.mark.parametrize(
    ("verb_name", "target", "key", "value"),
    [
        ("pr-view", f"{REPO}#77", "head_sha", HEAD),
        ("pr-status", f"{REPO}#77", "class", "pending"),
        ("pr-checks", f"{REPO}#77", "pending", ["CI Summary"]),
        ("pr-list", REPO, "count", 1),
        ("pr-merge-commit", f"{REPO}#70", "merge_commit_sha", "c" * 40),
    ],
)
def test_state_reads_are_answered_from_the_watcher_before_the_bus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb_name: str,
    target: str,
    key: str,
    value: object,
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path)
    result = _run([verb_name, target, "--watcher-state", str(state)])
    assert result.exit_code == 0, result.output
    answer = _answer(result)
    assert answer["source"] == "watcher"
    assert answer["bus"] is None
    assert answer["result"][key] == value
    assert bus.published == []


def test_pr_view_leaves_the_body_out_of_the_local_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeBus())
    state = _watcher(tmp_path)
    answer = _answer(_run(["pr-view", f"{REPO}#77", "--watcher-state", str(state)]))
    assert "body" not in answer["result"]


def test_stale_watcher_sends_an_open_pr_read_to_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path, age=timedelta(minutes=30))
    result = _run(["pr-status", f"{REPO}#77", "--watcher-state", str(state)])
    assert result.exit_code == 0, result.output
    assert _answer(result)["source"] == "bus"
    assert [topic for topic, _ in bus.published] == [GATEWAY.command]


def test_stale_watcher_still_answers_a_merged_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path, age=timedelta(days=2))
    result = _run(["pr-view", f"{REPO}#70", "--watcher-state", str(state)])
    assert _answer(result)["source"] == "watcher"
    assert bus.published == []


def test_source_local_never_asks_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    state = _watcher(tmp_path, age=timedelta(minutes=30))
    result = CliRunner().invoke(
        scm_cli.scm_group,
        ["pr-checks", f"{REPO}#77", "--source", "local", "--watcher-state", str(state)],
    )
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert bus.published == []


def test_merge_commit_is_answered_from_the_canonical_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    clone = tmp_path / "omnimarket"
    clone.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            },
        ).stdout.strip()

    git("init", "-q", "-b", "dev")
    git("commit", "-q", "--allow-empty", "-m", "feat: other (#12)")
    git("commit", "-q", "--allow-empty", "-m", "feat(OMN-1): the change (#88)")
    sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/dev", sha)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/dev")

    result = _run(
        [
            "pr-merge-commit",
            f"{REPO}#88",
            "--watcher-state",
            str(tmp_path / "absent.json"),
            "--clone",
            str(clone),
        ]
    )
    answer = _answer(result)
    assert answer["source"] == "clone"
    assert answer["result"]["merge_commit_sha"] == sha
    assert bus.published == []


# ------------------------------------------------- pr-heads (OMN-20977)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def _git_in(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(_GIT_ENV),
    ).stdout.strip()


def _origin_and_clone(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A bare origin with PR head refs and a clone whose dev history merged #88.

    #88 was squash-merged (its head branch deleted); #89 is open on a live
    branch; #90 was never merged and its branch is gone.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git_in(work, "init", "-q", "-b", "dev")
    _git_in(work, "commit", "-q", "--allow-empty", "-m", "feat: base (#12)")
    base = _git_in(work, "rev-parse", "HEAD")
    heads: dict[str, str] = {}
    for number, branch in (("88", "jonah/omn-1-merged"), ("89", "jonah/omn-1-open")):
        _git_in(work, "checkout", "-q", "-b", branch, base)
        _git_in(work, "commit", "-q", "--allow-empty", "-m", f"work {number}")
        heads[number] = _git_in(work, "rev-parse", "HEAD")
    _git_in(work, "checkout", "-q", "-b", "jonah/omn-1-dropped", base)
    _git_in(work, "commit", "-q", "--allow-empty", "-m", "work 90")
    heads["90"] = _git_in(work, "rev-parse", "HEAD")
    _git_in(work, "checkout", "-q", "dev")
    _git_in(work, "commit", "-q", "--allow-empty", "-m", "feat(OMN-1): merged (#88)")
    heads["merge"] = _git_in(work, "rev-parse", "HEAD")

    origin = tmp_path / "origin.git"
    _git_in(tmp_path, "init", "-q", "--bare", str(origin))
    _git_in(work, "push", "-q", str(origin), "dev", "jonah/omn-1-open")
    for number in ("88", "89", "90"):
        _git_in(
            work, "push", "-q", str(origin), f"{heads[number]}:refs/pull/{number}/head"
        )
    clone = tmp_path / "omnimarket"
    _git_in(tmp_path, "clone", "-q", "-b", "dev", str(origin), str(clone))
    return clone, heads


def test_pr_heads_is_answered_from_the_canonical_clone_over_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    clone, heads = _origin_and_clone(tmp_path)

    result = _run(
        [
            "pr-heads",
            REPO,
            "--source",
            "local",
            "--clone",
            str(clone),
            "--watcher-state",
            str(tmp_path / "absent.json"),
        ]
    )

    assert result.exit_code == 0, result.output
    answer = _answer(result)
    assert answer["source"] == "clone"
    assert answer["bus"] is None
    assert bus.published == []
    prs = {pr["number"]: pr for pr in answer["result"]["prs"]}
    assert answer["result"]["operation"] == "pr_heads"
    assert prs[88]["state"] == "MERGED"
    assert prs[88]["head_oid"] == heads["88"]
    assert prs[88]["merge_commit_sha"] == heads["merge"]
    # the merged branch is gone from origin, so the clone cannot name it
    assert prs[88]["head_ref"] is None
    # a clone cannot tell open from closed: never merged reads UNMERGED
    assert prs[89]["state"] == "UNMERGED"
    assert prs[89]["head_ref"] == "jonah/omn-1-open"
    assert prs[90]["state"] == "UNMERGED"
    assert prs[90]["merge_commit_sha"] is None


def test_pr_heads_overlays_the_watcher_and_narrows_by_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeBus())
    clone, heads = _origin_and_clone(tmp_path)
    tick = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "schema": 1,
                "last_tick": tick,
                "prs": {
                    "omnimarket#89": {
                        "facts": {
                            "number": 89,
                            "state": "OPEN",
                            "head_ref": "jonah/omn-1-open",
                            "head_sha": heads["89"],
                            "base": "dev",
                        }
                    }
                },
                "merges": {
                    "omnimarket#88": {
                        "head_ref": "jonah/omn-1-merged",
                        "head_sha": heads["88"],
                        "merge_sha": heads["merge"],
                        "merged_at": tick,
                        "base": "dev",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    base = ["pr-heads", REPO, "--source", "local", "--clone", str(clone)]

    answer = _answer(_run([*base, "--watcher-state", str(state)]))
    prs = {pr["number"]: pr for pr in answer["result"]["prs"]}
    assert prs[88]["head_ref"] == "jonah/omn-1-merged"
    assert prs[88]["merged_at"] == tick
    assert prs[89]["state"] == "OPEN"
    assert answer["watcher_tick"] == tick

    narrowed = _answer(
        _run([*base, "--watcher-state", str(state), "--head-sha", heads["88"]])
    )
    assert [pr["number"] for pr in narrowed["result"]["prs"]] == [88]


def test_pr_heads_without_a_clone_is_unanswered_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    result = CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-heads",
            REPO,
            "--source",
            "local",
            "--watcher-state",
            str(tmp_path / "absent.json"),
        ],
    )
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "no canonical clone" in result.output
    assert bus.published == []


# --------------------------------------------------------- refused verbs


@pytest.mark.parametrize("verb_name", sorted(r.verb for r in SCM_REFUSED_VERBS))
def test_verbs_with_no_node_operation_name_the_landing_controller(
    monkeypatch: pytest.MonkeyPatch, verb_name: str
) -> None:
    bus = FakeBus()
    _install(monkeypatch, bus)
    result = CliRunner().invoke(
        scm_cli.scm_group, [verb_name, f"{REPO}#77", "--squash"]
    )
    assert result.exit_code == scm_cli.EXIT_USAGE
    assert "landing controller" in result.output
    assert "/omni:pr-handoff" in result.output
    assert bus.published == []


def test_verbs_table_lists_served_and_refused() -> None:
    result = _run(["verbs"])
    table = json.loads(result.stdout)
    assert {v["verb"] for v in table["served"]} == {s.verb for s in SCM_VERBS}
    assert {v["verb"] for v in table["refused"]} == {r.verb for r in SCM_REFUSED_VERBS}
