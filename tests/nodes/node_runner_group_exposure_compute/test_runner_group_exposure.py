# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_compute: golden chain, error chain and named failing controls (OMN-22478)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_exposure_compute import (
    NodeRunnerGroupExposureCompute,
)
from omnimarket.nodes.node_runner_group_exposure_compute.handlers.handler_runner_group_exposure import (
    SUBJECT_ALLOWS_PUBLIC,
    SUBJECT_PINNED_EQUALS_FILES,
    SUBJECT_PUBLIC_JOBS_WINDOW,
    SUBJECT_PUBLIC_REACH_FILES,
    SUBJECT_READ_AGE,
    SUBJECTS,
    HandlerRunnerGroupExposure,
)
from omnimarket.nodes.node_runner_group_exposure_compute.models.model_runner_group_exposure import (
    EnumExposureReadFailure,
    ModelExposureSettings,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupExposureRequest,
    ModelRunnerGroupFacts,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SETTINGS = ModelExposureSettings(
    public_reach_events=(
        "pull_request",
        "pull_request_target",
        "issue_comment",
        "workflow_run",
    ),
    max_age_seconds=1800,
)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_runner_group_exposure_compute"
)
REPO = "OmniNode-ai/example-repo"


def _file(
    name: str, triggers: tuple[str, ...], *, selects: bool = True
) -> ModelGroupWorkflowFile:
    return ModelGroupWorkflowFile(
        repository=REPO,
        path=f".github/workflows/{name}",
        ref="refs/heads/dev",
        selects_group_label=selects,
        triggers=triggers,
    )


SAFE_FILES = (
    _file("a.yml", ("schedule", "workflow_dispatch")),
    _file("b.yml", ("push",)),
)
SAFE_PINS = tuple(f.identity for f in SAFE_FILES)


def _request(**changes: object) -> ModelRunnerGroupExposureRequest:
    fields: dict[str, object] = {
        "correlation_id": "corr-1",
        "now": NOW,
        "read_at": NOW - timedelta(seconds=60),
        "settings": SETTINGS,
        "group": ModelRunnerGroupFacts(
            group_id=7,
            allows_public_repositories=False,
            restricted_to_workflows=True,
            selected_workflows=SAFE_PINS,
            repositories=(REPO,),
        ),
        "files": SAFE_FILES,
        "jobs": (ModelGroupJob(run_id=1, run_event="schedule", workflow_ref="a"),),
    }
    fields.update(changes)
    return ModelRunnerGroupExposureRequest.model_validate(fields)


def _verdicts(
    request: ModelRunnerGroupExposureRequest,
) -> dict[str, EnumReadingVerdict]:
    reading = HandlerRunnerGroupExposure().handle(request)
    assert tuple(s.name for s in reading.subjects) == SUBJECTS
    return {s.name: s.verdict for s in reading.subjects}


def test_golden_chain_a_safe_group_reads_working_on_every_subject() -> None:
    reading = NodeRunnerGroupExposureCompute().handle(_request())
    assert reading.correlation_id == "corr-1"
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert all(s.verdict is EnumReadingVerdict.WORKING for s in reading.subjects)
    assert reading.read_failure is None


@pytest.mark.parametrize("failure", list(EnumExposureReadFailure))
def test_error_chain_a_refused_read_reads_unknown_with_its_typed_reason(
    failure: EnumExposureReadFailure,
) -> None:
    reading = HandlerRunnerGroupExposure().handle(
        _request(group=None, files=(), jobs=(), read_failure=failure)
    )
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert reading.read_failure is failure
    assert {s.verdict for s in reading.subjects} == {EnumReadingVerdict.UNKNOWN}
    assert all(failure.value in s.reason for s in reading.subjects)


def test_error_chain_no_group_facts_and_no_failure_reads_unknown_never_working() -> (
    None
):
    reading = HandlerRunnerGroupExposure().handle(_request(group=None))
    assert reading.verdict is EnumReadingVerdict.UNKNOWN
    assert all("no-group-facts" in s.reason for s in reading.subjects)


def test_control_inject_allows_public_true_reads_not_working() -> None:
    group = ModelRunnerGroupFacts(
        group_id=7,
        allows_public_repositories=True,
        restricted_to_workflows=True,
        selected_workflows=SAFE_PINS,
    )
    reading = HandlerRunnerGroupExposure().handle(_request(group=group))
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert (
        _verdicts(_request(group=group))[SUBJECT_ALLOWS_PUBLIC]
        is EnumReadingVerdict.NOT_WORKING
    )


def test_control_inject_planted_pr_file_reads_count_one() -> None:
    planted = _file("planted.yml", ("pull_request",))
    request = _request(
        files=(*SAFE_FILES, planted),
        group=ModelRunnerGroupFacts(
            group_id=7,
            allows_public_repositories=False,
            restricted_to_workflows=True,
            selected_workflows=(*SAFE_PINS, planted.identity),
        ),
    )
    reading = HandlerRunnerGroupExposure().handle(request)
    subject = next(s for s in reading.subjects if s.name == SUBJECT_PUBLIC_REACH_FILES)
    assert subject.count == 1
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert "planted.yml" in subject.reason
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING


def test_control_a_pull_request_file_that_selects_no_group_label_is_not_counted() -> (
    None
):
    other = _file("other.yml", ("pull_request",), selects=False)
    verdicts = _verdicts(_request(files=(*SAFE_FILES, other)))
    assert verdicts[SUBJECT_PUBLIC_REACH_FILES] is EnumReadingVerdict.WORKING
    assert verdicts[SUBJECT_PINNED_EQUALS_FILES] is EnumReadingVerdict.WORKING


def test_control_inject_unpinned_file_reads_not_working() -> None:
    unpinned = _file("unpinned.yml", ("workflow_dispatch",))
    reading = HandlerRunnerGroupExposure().handle(
        _request(files=(*SAFE_FILES, unpinned))
    )
    subject = next(s for s in reading.subjects if s.name == SUBJECT_PINNED_EQUALS_FILES)
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1
    assert "unpinned.yml" in subject.reason


def test_control_a_pin_that_matches_no_file_reads_not_working() -> None:
    group = ModelRunnerGroupFacts(
        group_id=7,
        allows_public_repositories=False,
        restricted_to_workflows=True,
        selected_workflows=(
            *SAFE_PINS,
            f"{REPO}/.github/workflows/gone.yml@refs/heads/dev",
        ),
    )
    subject = next(
        s
        for s in HandlerRunnerGroupExposure().handle(_request(group=group)).subjects
        if s.name == SUBJECT_PINNED_EQUALS_FILES
    )
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert "gone.yml" in subject.reason


def test_control_an_unrestricted_group_reads_not_working_on_the_pins() -> None:
    group = ModelRunnerGroupFacts(
        group_id=7,
        allows_public_repositories=False,
        restricted_to_workflows=False,
    )
    assert (
        _verdicts(_request(group=group))[SUBJECT_PINNED_EQUALS_FILES]
        is EnumReadingVerdict.NOT_WORKING
    )


@pytest.mark.parametrize(
    "job",
    [
        ModelGroupJob(run_id=2, run_event="pull_request", workflow_ref="a"),
        ModelGroupJob(run_id=3, run_event="push", head_is_fork=True, workflow_ref="a"),
    ],
)
def test_control_a_public_origin_job_in_the_window_reads_not_working(
    job: ModelGroupJob,
) -> None:
    request = _request(jobs=(*_request().jobs, job))
    reading = HandlerRunnerGroupExposure().handle(request)
    subject = next(s for s in reading.subjects if s.name == SUBJECT_PUBLIC_JOBS_WINDOW)
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1


def test_a_stale_read_is_unknown_overall_even_when_a_subject_is_not_working() -> None:
    stale = NOW - timedelta(seconds=SETTINGS.max_age_seconds + 1)
    assert (
        HandlerRunnerGroupExposure().handle(_request(read_at=stale)).verdict
        is EnumReadingVerdict.UNKNOWN
    )
    assert (
        _verdicts(_request(read_at=stale))[SUBJECT_READ_AGE]
        is EnumReadingVerdict.UNKNOWN
    )
    on_the_limit = NOW - timedelta(seconds=SETTINGS.max_age_seconds)
    assert (
        HandlerRunnerGroupExposure().handle(_request(read_at=on_the_limit)).verdict
        is EnumReadingVerdict.WORKING
    )


def test_a_read_dated_after_now_is_unknown() -> None:
    future = NOW + timedelta(seconds=1)
    assert (
        _verdicts(_request(read_at=future))[SUBJECT_READ_AGE]
        is EnumReadingVerdict.UNKNOWN
    )


def test_the_contract_declares_the_topics_and_holds_no_policy_value() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    command = "onex.cmd.omnimarket.runner-group-exposure-read-requested.v1"
    terminal = "onex.evt.omnimarket.runner-group-exposure-read.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["terminal_event"] == terminal
    text = (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    assert "max_age_seconds:" not in text
    assert "pull_request_target" not in text
