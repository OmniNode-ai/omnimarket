# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real gateway adapter of the nightly miss collector, run under the real handlers over a typed fake gateway.

The fake gateway answers the repository gateway's typed read results (workflow runs, run jobs, artifacts with real
zip archives, pull request heads), so everything from the request the adapter builds to the alarm the handler
raises is the production code. What it does not prove is the live GitHub transport; that is the gateway node's own
tests and the live read of a real night.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_gateway_node_skew import (
    GatewayReadError as GatewayClientError,
)
from omnimarket.models.model_github_gateway_io import (
    EnumArtifactFetchRefusal,
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelArtifactContent,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelPrHead,
    ModelPrHeadsResult,
    ModelRunJob,
    ModelRunJobsResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.models.test_selector_reading import (
    EnumAttributionClass,
    EnumEvidenceState,
    EnumFileOutcome,
    EnumIncompleteReason,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.gateway_adapter import (
    GatewayNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.git_commit_range import (
    GitCommitRange,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.junit_outcomes import (
    JunitUnreadableError,
    file_of_dotted,
    read_junit,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers import (
    HandlerCollectNightly,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    EnumAlarmKind,
    EnumCollectStatus,
    ModelCollectRequest,
    ModelMissCollectSettings,
    ModelRangeCommit,
    load_policy,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
)

pytestmark = pytest.mark.unit

REPO = "example-org/example-repo"
SLOT = date(2026, 11, 21)
NOW = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
POLICY = load_policy().model_copy(
    update={"nightly_shard_count": 2, "nightly_min_test_files": 3}
)
SETTINGS = ModelMissCollectSettings.load(
    {
        "repo": REPO,
        "workflow_file": "nightly-full-suite.yml",
        "owner_role": "example-test-selection-owner",
        "alarm_route": "test_selector_miss",
        "collector_enabled": True,
        "status_tick": "example-lab-status-tick",
    }
)
RED = "tests/nodes/node_x/test_x.py"
TODAY_RUN, YESTERDAY_RUN, PR_RUN = 9001, 9000, 7001
PREVIOUS_HEAD, HEAD, MERGE_SHA, PR_HEAD, CHECKOUT = (
    "a" * 40,
    "b" * 40,
    "c" * 40,
    "d" * 40,
    "e" * 40,
)
MERGED = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _junit(cases: dict[str, str]) -> bytes:
    """A junit report: classname -> ``passed``, ``failed``, ``skipped`` or ``collection``."""
    rows = []
    for module, kind in cases.items():
        dotted = module.removesuffix(".py").replace("/", ".")
        if kind == "collection":
            rows.append(
                f'<testcase classname="" name="{dotted}"><error message="collection failure">x</error></testcase>'
            )
            continue
        body = {
            "passed": "",
            "failed": '<failure message="assert 0">x</failure>',
            "skipped": '<skipped message="s"/>',
        }[kind]
        rows.append(f'<testcase classname="{dotted}" name="test_a">{body}</testcase>')
    return f"<testsuites><testsuite>{''.join(rows)}</testsuite></testsuites>".encode()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGateway:
    """Answers the typed read results of the repository gateway and counts each operation."""

    def __init__(self) -> None:
        self.nightly: list[ModelWorkflowRunSummary] = []
        self.pr_runs: list[ModelWorkflowRunSummary] = []
        self.jobs: dict[int, list[ModelRunJob]] = {}
        self.artifacts: dict[int, dict[str, bytes]] = {}
        self.pr_heads: list[ModelPrHead] = []
        self.refuse: set[int] = set()
        self.pr_heads_truncated = False
        self.fail_all = False
        self.calls: list[str] = []

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        self.calls.append(request.operation.value)
        if self.fail_all:
            raise GatewayClientError("the gateway did not answer")
        match request.operation:
            case EnumGithubGatewayOperation.WORKFLOW_RUNS:
                if request.workflow == "nightly-full-suite.yml":
                    return ModelWorkflowRunsResult(
                        repo=REPO, runs=self.nightly, truncated=False
                    )
                runs = [r for r in self.pr_runs if r.head_sha == request.head_sha]
                return ModelWorkflowRunsResult(repo=REPO, runs=runs, truncated=False)
            case EnumGithubGatewayOperation.RUN_JOBS:
                return ModelRunJobsResult(
                    repo=REPO,
                    run_id=request.run_id or 1,
                    jobs=self.jobs.get(request.run_id or 0, []),
                    truncated=False,
                )
            case EnumGithubGatewayOperation.ARTIFACTS:
                run_id = request.run_id or 0
                held = self.artifacts.get(run_id, {})
                listing = [
                    ModelArtifactSummary(
                        id=run_id * 100 + index,
                        name=name,
                        size_in_bytes=len(data),
                        expired=False,
                    )
                    for index, (name, data) in enumerate(held.items())
                ]
                if request.artifact_id is None:
                    return ModelArtifactsResult(
                        repo=REPO, run_id=run_id, artifacts=listing, truncated=False
                    )
                if request.artifact_id in self.refuse:
                    return ModelArtifactsResult(
                        repo=REPO,
                        run_id=run_id,
                        artifacts=listing,
                        truncated=False,
                        fetch_refused=EnumArtifactFetchRefusal.OVER_SIZE_CAP,
                    )
                summary = next(a for a in listing if a.id == request.artifact_id)
                data = held[summary.name]
                return ModelArtifactsResult(
                    repo=REPO,
                    run_id=run_id,
                    artifacts=listing,
                    truncated=False,
                    fetched=ModelArtifactContent(
                        artifact_id=summary.id,
                        name=summary.name,
                        size_bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                        content_base64=base64.b64encode(data).decode(),
                    ),
                )
            case EnumGithubGatewayOperation.PR_HEADS:
                return ModelPrHeadsResult(
                    repo=REPO, prs=self.pr_heads, truncated=self.pr_heads_truncated
                )
        raise AssertionError(f"unexpected operation {request.operation}")


class FakeCommitRange:
    def __init__(self, *commits: ModelRangeCommit) -> None:
        self.commits = commits

    def first_parent(
        self, previous_head: str, head: str
    ) -> tuple[ModelRangeCommit, ...]:
        assert (previous_head, head) == (PREVIOUS_HEAD, HEAD)
        return self.commits


def _run(
    run_id: int, created: datetime, head_sha: str, status: str = "completed"
) -> ModelWorkflowRunSummary:
    return ModelWorkflowRunSummary(
        id=run_id,
        name="run",
        workflow_id=1,
        head_sha=head_sha,
        event="schedule",
        status=status,
        conclusion="success" if status == "completed" else None,
        run_number=run_id,
        run_attempt=1,
        created_at=_stamp(created),
        updated_at=_stamp(created),
        html_url="https://example.invalid/run",
    )


def _job(
    name: str, finished: datetime, conclusion: str = "success", attempt: int = 1
) -> ModelRunJob:
    return ModelRunJob(
        id=abs(hash((name, attempt))) % 10**9 + 1,
        name=name,
        status="completed",
        conclusion=conclusion,
        run_attempt=attempt,
        completed_at=_stamp(finished),
    )


def _record(**over: Any) -> bytes:
    selected = over.pop("selected", ["tests/nodes/node_y/"])
    document: dict[str, Any] = {
        "pr_number": 7,
        "head_sha": PR_HEAD,
        "checkout_sha": CHECKOUT,
        "run_id": PR_RUN,
        "run_attempt": 1,
        "executed_selection": {"selected_paths": selected, "split_count": 1},
        "selection_by_stage": {"0": {"selected_paths": ["tests/"], "split_count": 1}},
        "unread_key": "ignored",
        **over,
    }
    return _zip({"selection-record.json": json.dumps(document).encode()})


def _world(
    *,
    tonight: dict[str, str] | None = None,
    shards: int = 2,
    record: bytes | None = None,
    pr_finished: datetime | None = None,
    in_range: bool = True,
    pr_listed: bool = True,
) -> tuple[FakeGateway, FakeCommitRange]:
    """A night whose RED file is red after being green, with one merged pull request between the two heads."""
    gateway = FakeGateway()
    morning = datetime.combine(SLOT, datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=2
    )
    gateway.nightly = [
        _run(TODAY_RUN, morning, HEAD),
        _run(YESTERDAY_RUN, morning - timedelta(days=1), PREVIOUS_HEAD),
    ]
    today = tonight or {
        RED: "failed",
        "tests/a/test_a.py": "passed",
        "tests/b/test_b.py": "passed",
    }
    green = {
        RED: "passed",
        "tests/a/test_a.py": "passed",
        "tests/b/test_b.py": "passed",
    }
    for run_id, files in ((TODAY_RUN, today), (YESTERDAY_RUN, green)):
        names = list(files)
        gateway.artifacts[run_id] = {
            f"nightly-test-results-{n}": _zip(
                {f"junit-{n}.xml": _junit({k: files[k] for k in names[n - 1 :: 2]})}
            )
            for n in range(1, shards + 1)
        }
    gateway.pr_runs = [_run(PR_RUN, MERGED - timedelta(hours=1), PR_HEAD)]
    finished = pr_finished or MERGED - timedelta(minutes=5)
    gateway.jobs[PR_RUN] = [
        _job("Tests (Split 1/1)", finished),
        _job("Detect changes", finished),
    ]
    gateway.artifacts[PR_RUN] = {
        f"test-selection-7-{PR_RUN}-1": record if record is not None else _record()
    }
    if pr_listed:
        gateway.pr_heads = [
            ModelPrHead(
                number=7,
                state="MERGED",
                head_oid=PR_HEAD,
                merge_commit_sha=MERGE_SHA,
                merged_at=_stamp(MERGED),
            )
        ]
    commits = (
        (ModelRangeCommit(sha=MERGE_SHA, committed_at=MERGED),) if in_range else ()
    )
    return gateway, FakeCommitRange(*commits)


def _collect(
    gateway: FakeGateway, commits: FakeCommitRange
) -> tuple[Any, InMemoryAlarmStore, InMemoryAlarmReceiver]:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    handler = HandlerCollectNightly(
        settings=SETTINGS,
        policy=POLICY,
        gateway=GatewayNightlyGateway(
            reader=gateway, commits=commits, settings=SETTINGS, policy=POLICY
        ),
        store=store,
        receiver=receiver,
    )
    return handler.handle(ModelCollectRequest(slot_date=SLOT, now=NOW)), store, receiver


# --- the injected miss, through the real adapter (AC6) ---------------------------------------------


def test_injected_miss_through_the_gateway_is_classed_miss_and_raises_one_alarm() -> (
    None
):
    gateway, commits = _world()
    result, store, receiver = _collect(gateway, commits)
    assert result.status is EnumCollectStatus.COLLECTED
    (item,) = [a for a in result.attribution.attributions if a.test_file == RED]
    assert item.klass is EnumAttributionClass.MISS
    assert item.suspects == (MERGE_SHA,)
    assert (len(store.rows), len(receiver.received)) == (1, 1)
    (alarm, status) = next(iter(store.rows.values()))
    assert (alarm.kind, status) == (EnumAlarmKind.MISS, "delivered")
    assert {"workflow_runs", "run_jobs", "artifacts", "pr_heads"} <= set(gateway.calls)


def test_injected_miss_the_same_night_with_the_test_selected_raises_none() -> None:
    gateway, commits = _world(record=_record(selected=["tests/nodes/node_x/"]))
    result, store, receiver = _collect(gateway, commits)
    (item,) = [a for a in result.attribution.attributions if a.test_file == RED]
    assert item.klass is EnumAttributionClass.INTERACTION
    assert (len(store.rows), len(receiver.received)) == (0, 0)


# --- no evidence is never a pass, through the real adapter (AC2) ---------------------------------------


@pytest.mark.parametrize(
    ("world", "state"),
    [
        (
            {"record": _zip({"selection-record.json": b"{not json"})},
            EnumEvidenceState.MISSING_RECORD,
        ),
        (
            {"record": _record(run_id=1)},
            EnumEvidenceState.MISSING_RECORD,
        ),
        (
            {"record": _record(head_sha="f" * 40)},
            EnumEvidenceState.MISSING_RECORD,
        ),
        (
            {
                "pr_finished": MERGED + timedelta(minutes=5),
                "record": _record(selected=["tests/nodes/node_x/"]),
            },
            EnumEvidenceState.FINISHED_AFTER_MERGE,
        ),
    ],
)
def test_no_evidence_through_the_gateway_is_unattributable_with_a_reason(
    world: dict[str, Any], state: EnumEvidenceState
) -> None:
    # Positive control: the same night with a record that selects the red file and a timely run is interaction.
    control, control_commits = _world(record=_record(selected=["tests/nodes/node_x/"]))
    (clean,) = [
        a
        for a in _collect(control, control_commits)[0].attribution.attributions
        if a.test_file == RED
    ]
    assert clean.klass is EnumAttributionClass.INTERACTION
    gateway, commits = _world(**world)
    result, store, _ = _collect(gateway, commits)
    (item,) = [a for a in result.attribution.attributions if a.test_file == RED]
    assert item.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert [e.state for e in item.no_evidence] == [state]
    assert len(store.rows) == 1


def test_a_commit_with_no_pull_request_is_a_direct_push() -> None:
    gateway, commits = _world(pr_listed=False)
    result, _, _ = _collect(gateway, commits)
    (item,) = [a for a in result.attribution.attributions if a.test_file == RED]
    assert item.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert [e.state for e in item.no_evidence] == [EnumEvidenceState.DIRECT_PUSH]


def test_a_cut_pull_request_list_never_reads_an_unlisted_commit_as_a_direct_push() -> (
    None
):
    gateway, commits = _world(pr_listed=False)
    gateway.pr_heads_truncated = True
    result, store, _ = _collect(gateway, commits)
    assert result.status is EnumCollectStatus.READ_FAILED
    assert "cut" in result.failure
    assert len(store.rows) == 0


def test_an_empty_commit_range_has_no_candidate() -> None:
    gateway, commits = _world(in_range=False)
    result, _, _ = _collect(gateway, commits)
    (item,) = [a for a in result.attribution.attributions if a.test_file == RED]
    assert item.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert "pr_heads" not in gateway.calls


# --- completeness, through the real adapter (AC3) ---------------------------------------------------------


def test_fewer_artifacts_than_the_shard_count_raises_one_incomplete_alarm() -> None:
    gateway, commits = _world(shards=1)
    result, store, receiver = _collect(gateway, commits)
    assert (
        EnumIncompleteReason.MISSING_ARTIFACTS in result.attribution.incomplete_reasons
    )
    ((alarm, _),) = store.rows.values()
    assert alarm.kind is EnumAlarmKind.INCOMPLETE
    assert len(receiver.received) == 1


def test_an_artifact_the_gateway_refuses_is_a_shard_in_error() -> None:
    gateway, commits = _world()
    gateway.refuse = {TODAY_RUN * 100}
    result, store, _ = _collect(gateway, commits)
    assert EnumIncompleteReason.SHARD_IN_ERROR in result.attribution.incomplete_reasons
    assert len(store.rows) == 1


def test_a_collection_error_in_a_shard_is_a_shard_in_error() -> None:
    gateway, commits = _world(
        tonight={
            RED: "collection",
            "tests/a/test_a.py": "passed",
            "tests/b/test_b.py": "passed",
        }
    )
    result, store, _ = _collect(gateway, commits)
    assert EnumIncompleteReason.SHARD_IN_ERROR in result.attribution.incomplete_reasons
    ((alarm, _),) = store.rows.values()
    assert alarm.kind is EnumAlarmKind.INCOMPLETE


def test_a_gateway_that_does_not_answer_records_nothing() -> None:
    gateway, commits = _world()
    gateway.fail_all = True
    result, store, _ = _collect(gateway, commits)
    assert result.status is EnumCollectStatus.READ_FAILED
    assert len(store.rows) == 0
    assert not store.has_collector_result(REPO, SLOT)


def test_no_nightly_run_on_the_slot_date_is_no_run() -> None:
    gateway, commits = _world()
    gateway.nightly = gateway.nightly[1:]
    result, _, _ = _collect(gateway, commits)
    assert result.status is EnumCollectStatus.NO_RUN


# --- the junit reader ----------------------------------------------------------------------------------


def test_junit_outcomes_take_the_worst_outcome_per_file() -> None:
    document = (
        b"<testsuite>"
        b'<testcase classname="tests.t.test_a.TestX" name="a"/>'
        b'<testcase classname="tests.t.test_a.TestX" name="b"><failure message="m">x</failure></testcase>'
        b'<testcase classname="tests.t.test_b" name="c"><skipped message="s"/></testcase>'
        b"</testsuite>"
    )
    outcomes, collection = read_junit(document)
    assert outcomes == {
        "tests/t/test_a.py": EnumFileOutcome.FAILED,
        "tests/t/test_b.py": EnumFileOutcome.SKIPPED,
    }
    assert collection is False


@pytest.mark.parametrize(
    "document",
    [
        b"<html/>",
        b"not xml",
        b'<testsuite><testcase classname="nodots" name="a"/></testsuite>',
    ],
)
def test_junit_that_is_not_a_report_or_unplaceable_is_refused(document: bytes) -> None:
    with pytest.raises(JunitUnreadableError):
        read_junit(document)


def test_a_dotted_name_with_no_module_segment_is_not_placed() -> None:
    assert (
        file_of_dotted("tests.nodes.node_x.test_y.TestZ")
        == "tests/nodes/node_x/test_y.py"
    )
    assert file_of_dotted("tests.conftest") == "tests/conftest.py"
    assert file_of_dotted("tests.nodes.helper") is None


# --- the commit range against a real clone ----------------------------------------------------------------


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(where),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_first_parent_range_lists_the_merge_and_not_the_side_commits(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f").write_text("0")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "base")
    previous = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "side")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "commit", "-q", "-am", "side commit")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "merge", "side")
    merge = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "g").write_text("2")
    _git(tmp_path, "add", "g")
    _git(tmp_path, "commit", "-q", "-m", "direct push")
    head = _git(tmp_path, "rev-parse", "HEAD")
    listed = GitCommitRange(tmp_path).first_parent(previous, head)
    assert [c.sha for c in listed] == [merge, head]
    assert all(c.committed_at.tzinfo is not None for c in listed)
    assert GitCommitRange(tmp_path).first_parent(head, head) == ()


def test_first_parent_range_refuses_a_ref_that_is_not_a_commit_id(
    tmp_path: Path,
) -> None:
    with pytest.raises(GatewayReadError, match="not a commit id"):
        GitCommitRange(tmp_path).first_parent("--output=x", "a" * 40)
    with pytest.raises(GatewayReadError, match="git log failed"):
        GitCommitRange(tmp_path).first_parent("a" * 40, "b" * 40)
