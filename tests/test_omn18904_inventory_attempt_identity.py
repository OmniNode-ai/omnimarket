# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inventory producer identity, rerun rescue and projection seam proofs."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    HandlerPrLifecycleInventory,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelPrInventoryInput,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers import (
    CiAttemptOutcomeProjectionWriter,
)

pytestmark = pytest.mark.unit
REPO = "OmniNode-ai/omnimarket"
HEADS = tuple(character * 40 for character in "abc")
IDENTITY = ("head_sha", "run_id", "run_attempt", "failed_step_name")


def _job(
    index: int = 0, *, attempt: int = 1, conclusion: str = "failure"
) -> dict[str, Any]:
    """Jobs-interface shape, with deterministic fixture identities."""
    return {
        "id": 900 + index,
        "run_id": 500 + index,
        "run_attempt": attempt,
        "head_sha": HEADS[index],
        "name": "tests",
        "status": "completed",
        "conclusion": conclusion,
        "steps": [
            {"name": "Set up job", "conclusion": "success"},
            {"name": "Run pytest suite", "conclusion": conclusion},
        ],
    }


def _check(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"],
        "state": "FAILURE",
        "bucket": "fail",
        "event": "pull_request",
        "link": f"https://github.com/{REPO}/actions/runs/{job['run_id']}/job/{job['id']}",
    }


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    jobs: list[dict[str, Any]],
    checks: list[dict[str, Any]] | None = None,
    unavailable: bool = False,
    commits: list[dict[str, Any]] | None = None,
    latest_jobs: list[dict[str, Any]] | None = None,
) -> tuple[HandlerPrLifecycleInventory, list[list[str]]]:
    calls: list[list[str]] = []

    def run(
        cmd: list[str], *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        payload: Any = {}
        code = 0
        if cmd[:3] == ["gh", "pr", "checks"]:
            payload = checks if checks is not None else [_check(jobs[0])]
        elif cmd[:3] == ["gh", "pr", "view"]:
            payload = (
                {"reviews": []}
                if cmd[-1] == "reviews"
                else {
                    "title": "fix(OMN-18904): attempt identity",
                    "state": "OPEN",
                    "headRefOid": HEADS[-1],
                }
            )
        elif cmd[:2] == ["gh", "api"]:
            endpoint = cmd[2]
            if "/pulls/" in endpoint and "/commits" in endpoint:
                payload = (
                    commits if commits is not None else [{"sha": sha} for sha in HEADS]
                )
            elif "/actions/runs/" in endpoint and "/jobs" in endpoint:
                run_id = int(endpoint.split("/runs/")[1].split("/")[0])
                source = jobs if latest_jobs is None else latest_jobs
                payload = {"jobs": [job for job in source if job["run_id"] == run_id]}
                code = 1 if unavailable else 0
            elif "/actions/jobs/" in endpoint and "/logs" not in endpoint:
                job_id = int(endpoint.rsplit("/", 1)[1])
                payload = next((job for job in jobs if job["id"] == job_id), {})
        return subprocess.CompletedProcess(
            cmd, code, json.dumps(payload), "unavailable" if code else ""
        )

    handler = HandlerPrLifecycleInventory()
    monkeypatch.setattr(handler, "_run_gh", run)
    monkeypatch.setattr(handler, "_collect_coderabbit_unresolved", lambda *_args: 0)
    return handler, calls


def test_failed_check_carries_identity_and_verdict_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    handler, _ = _handler(monkeypatch, jobs=[job])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=job["head_sha"])[0]
    wire = check.model_dump(mode="json")
    assert tuple(wire[field] for field in IDENTITY) == (
        job["head_sha"],
        str(job["run_id"]),
        job["run_attempt"],
        job["steps"][1]["name"],
    )
    assert check.reason_code is EnumMergeCheckReasonCode.PRODUCT_FAILED
    assert check.cause_affirmative is True


@pytest.mark.parametrize("rescued", [False, True])
def test_same_commit_later_green_attempt_rescues_product_step(
    monkeypatch: pytest.MonkeyPatch, rescued: bool
) -> None:
    failed = _job()
    jobs = [failed]
    if rescued:
        success = _job(attempt=2, conclusion="success")
        success["id"] = 999
        jobs.append(success)
    handler, _ = _handler(monkeypatch, jobs=jobs, checks=[_check(failed)])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=failed["head_sha"])[
        0
    ]
    expected = (
        EnumMergeCheckReasonCode.RUNNER_INFRA
        if rescued
        else EnumMergeCheckReasonCode.PRODUCT_FAILED
    )
    assert check.reason_code is expected
    assert check.run_attempt == 1
    assert check.head_sha == failed["head_sha"]


@pytest.mark.parametrize("difference", ["head_sha", "name", "run_id", "run_attempt"])
def test_other_commit_job_run_or_earlier_green_does_not_rescue(
    monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    failed = _job()
    success = _job(attempt=2, conclusion="success")
    success["id"] = 999
    success[difference] = {
        "head_sha": HEADS[1],
        "name": "other-job",
        "run_id": 700,
        "run_attempt": 1,
    }[difference]
    handler, _ = _handler(monkeypatch, jobs=[failed, success], checks=[_check(failed)])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=failed["head_sha"])[
        0
    ]
    assert check.reason_code is EnumMergeCheckReasonCode.PRODUCT_FAILED


@pytest.mark.parametrize("green", [False, True])
def test_unclassified_or_unavailable_check_has_no_identity(
    monkeypatch: pytest.MonkeyPatch, green: bool
) -> None:
    job = _job()
    raw = _check(job)
    if green:
        raw.update(state="SUCCESS", bucket="pass")
    handler, calls = _handler(
        monkeypatch, jobs=[job], checks=[raw], unavailable=not green
    )
    check = handler._collect_check_runs(REPO, 42, current_head_sha=job["head_sha"])[0]
    assert all(check.model_dump()[field] is None for field in IDENTITY)
    if green:
        assert check.reason_code is None
        assert not any("/actions/" in " ".join(call) for call in calls)
    else:
        assert check.reason_code is EnumMergeCheckReasonCode.RUNNER_INFRA
        assert check.cause_affirmative is False


def test_unknown_step_retains_nonaffirmative_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    job["steps"][1]["name"] = "Opaque command"
    handler, _ = _handler(monkeypatch, jobs=[job])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=job["head_sha"])[0]
    assert check.reason_code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert check.cause_affirmative is False
    assert check.run_attempt == 1


def test_latest_only_jobs_recovers_original_attempt_before_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _job()
    success = _job(attempt=2, conclusion="success")
    success["id"] = 999
    handler, calls = _handler(
        monkeypatch, jobs=[failed, success], latest_jobs=[success]
    )
    check = handler._collect_check_runs(REPO, 42, current_head_sha=failed["head_sha"])[
        0
    ]
    assert check.reason_code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert check.cause_affirmative is True
    assert check.run_attempt == failed["run_attempt"]
    assert check.failed_step_name == failed["steps"][1]["name"]
    assert ["gh", "api", f"repos/{REPO}/actions/jobs/{failed['id']}"] in calls


def test_missing_original_job_never_borrows_later_attempt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _job()
    success = _job(attempt=2, conclusion="success")
    success["id"] = 999
    handler, _ = _handler(monkeypatch, jobs=[success], checks=[_check(failed)])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=failed["head_sha"])[
        0
    ]
    assert check.reason_code is EnumMergeCheckReasonCode.STALE_CONTEXT
    assert all(check.model_dump()[field] is None for field in IDENTITY)


@pytest.mark.parametrize(
    ("field", "value"),
    [("head_sha", "bad-sha"), ("run_attempt", 0), ("run_attempt", True), ("run_id", 0)],
)
def test_invalid_job_identity_is_not_emitted(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    job = _job()
    raw = _check(job)
    job[field] = value
    handler, _ = _handler(monkeypatch, jobs=[job], checks=[raw])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=HEADS[0])[0]
    assert all(check.model_dump()[key] is None for key in IDENTITY)


def test_paginated_jobs_resolves_linked_job_on_second_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    handler, calls = _handler(monkeypatch, jobs=[job])
    original_run = handler._run_gh

    def run(
        cmd: list[str], *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        if cmd[2].endswith("/jobs"):
            calls.append(cmd)
            assert cmd[-2:] == ["--paginate", "--slurp"]
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps([{"jobs": []}, {"jobs": [job]}]), ""
            )
        return (
            original_run(cmd) if timeout is None else original_run(cmd, timeout=timeout)
        )

    monkeypatch.setattr(handler, "_run_gh", run)
    check = handler._collect_check_runs(REPO, 42, current_head_sha=HEADS[0])[0]
    assert check.run_id == str(job["run_id"])
    assert check.reason_code is EnumMergeCheckReasonCode.PRODUCT_FAILED


def test_commit_history_pagination_and_duplicates_keep_first_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, _ = _handler(monkeypatch, jobs=[_job()])

    def run(
        cmd: list[str], *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[-2:] == ["--paginate", "--slurp"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps([[{"sha": HEADS[0]}], [{"sha": HEADS[0]}, {"sha": HEADS[1]}]]),
            "",
        )

    monkeypatch.setattr(handler, "_run_gh", run)
    assert handler._collect_head_sha_history(REPO, 42) == HEADS[:2]


@pytest.mark.parametrize(
    ("payload", "returncode"),
    [
        ("not-json", 0),
        ("{}", 0),
        ('[{"sha":"bad"}]', 0),
        ('[[{"sha":"bad"}]]', 0),
        ("[]", 1),
    ],
)
def test_invalid_or_unavailable_history_never_guesses_an_ordinal(
    monkeypatch: pytest.MonkeyPatch, payload: str, returncode: int
) -> None:
    handler, _ = _handler(monkeypatch, jobs=[_job()])
    monkeypatch.setattr(
        handler,
        "_run_gh",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, returncode, payload, "unavailable"
        ),
    )
    assert handler._collect_head_sha_history(REPO, 42) == ()


def test_old_check_consumer_accepts_emitted_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Field shape from the inventory check model before consumer-first fields
    # landed at 39632bb76. Pydantic's default extra policy was ignore.
    class OldCheck(BaseModel):
        name: str
        status: str
        conclusion: str | None = None
        event: str | None = None
        link: str = ""
        flaky_failure_evidence: tuple[str, ...] = ()
        reason_code: EnumMergeCheckReasonCode | None = None

    job = _job()
    handler, _ = _handler(monkeypatch, jobs=[job])
    check = handler._collect_check_runs(REPO, 42, current_head_sha=job["head_sha"])[0]
    wire = check.model_dump(mode="json")
    assert wire["run_attempt"] == 1  # A replay without new fields proves nothing.
    old = OldCheck.model_validate(wire)
    assert old.reason_code is EnumMergeCheckReasonCode.PRODUCT_FAILED
    assert "run_attempt" not in old.model_dump()


def test_one_inventory_sweep_projects_three_heads_in_commit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [_job(index) for index in range(3)]
    handler, _ = _handler(
        monkeypatch, jobs=jobs, checks=[_check(job) for job in reversed(jobs)]
    )
    output = handler.handle(
        ModelPrInventoryInput(
            repo=REPO, pr_numbers=(42,), collect_org_wide_census=False
        )
    )
    assert output.collection_errors == ()
    assert output.pr_states[0].head_sha_history == HEADS

    class RecordingDatabase(AsyncpgAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[Any, ...]] = []

        async def connect(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def execute(
            self, query: str, *params: Any, tenant: str | None = None
        ) -> list[dict[str, Any]]:
            assert "INSERT INTO" in query
            assert tenant is None
            self.calls.append(params)
            return [{"projection_cursor": len(self.calls)}]

    writer = CiAttemptOutcomeProjectionWriter()
    assert writer.onex_runtime_inprocess_dispatch is True
    db = RecordingDatabase()
    writer._db = db
    event = output.model_dump(mode="json")
    event["_envelope_timestamp"] = datetime(2026, 10, 8, tzinfo=UTC).isoformat()
    projected = writer.handle(event)
    assert projected["rows_upserted"] == 3
    assert [row["attempt_ordinal"] for row in projected["attempt_rows"]] == [1, 2, 3]
    assert [params[2] for params in db.calls] == list(HEADS)
    assert [params[7] for params in db.calls] == [1, 2, 3]
