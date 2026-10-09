# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""External assertions follow producer triggers and push branches (OMN-18963).

The earlier fix disabled all L4 checks on push. This suite proves the applicable
push producers still gate, pins their declarations to real workflow YAML, and
preserves the integrated PR and merge-group admission sets.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate
from scripts.ci.ci_summary_gate import (
    EXPECTED_EXTERNAL_CONTEXTS,
    MERGE_ADMISSION_EVENTS,
    external_layer_applies,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("event", sorted(MERGE_ADMISSION_EVENTS))
def test_merge_admission_events_still_assert_the_layer(event: str) -> None:
    """The events on which a merge is actually admitted are unchanged."""

    assert external_layer_applies(event) is True


def test_push_retains_push_producers_and_excludes_pr_only_contexts() -> None:
    pushed = gate.expected_external_contexts("push", ref_name="main")
    assert "Git env scrub gate" not in pushed
    assert "Hostile Review Gate" not in pushed
    assert "Precommit Fail-Loud Gate" in pushed
    assert "handler-event-type-source" in pushed
    assert external_layer_applies("push") is True


def test_dev_push_does_not_wait_for_main_only_producers() -> None:
    pushed = gate.expected_external_contexts("push", ref_name="dev")
    assert set(pushed) == {
        "Enforce validator-requirements.yaml (OMN-13291)",
        "Precommit Fail-Loud Gate",
        "fsm-handler-drift",
        "validate",
    }


def _workflows() -> dict:
    root = Path(__file__).resolve().parents[4]
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in (root / ".github/workflows").glob("*.yml")
    }


def _assert_producer_parity(
    context: str, producer: gate.ExternalContextProducer, doc: dict
) -> None:
    triggers = doc.get("on", doc.get(True))
    assert set(producer.events) == set(triggers), (context, producer.workflow)
    push = triggers.get("push") or {}
    assert producer.push_branches == tuple(push.get("branches", ())), context
    assert not push.get("paths"), context
    assert not push.get("paths-ignore"), context
    assert not push.get("branches-ignore"), context
    assert not push.get("tags"), context
    assert all(not pattern.startswith("!") for pattern in producer.push_branches), (
        context
    )
    jobs = doc["jobs"]
    assert any(
        context == job.get("name", job_id)
        or ("uses" in job and context.startswith(str(job.get("name", job_id)) + " / "))
        for job_id, job in jobs.items()
    ), (context, producer.workflow)


def test_declared_events_and_push_filters_match_live_producers() -> None:
    workflows = _workflows()
    assert set(gate.EXTERNAL_CONTEXT_PRODUCERS) == set(EXPECTED_EXTERNAL_CONTEXTS)
    for context, producer in gate.EXTERNAL_CONTEXT_PRODUCERS.items():
        _assert_producer_parity(context, producer, workflows[producer.workflow])


@pytest.mark.parametrize("field", ["events", "push_branches"])
def test_parity_rejects_a_changed_declaration(field: str) -> None:
    context = "Precommit Fail-Loud Gate"
    producer = gate.EXTERNAL_CONTEXT_PRODUCERS[context]
    changed = replace(producer, **{field: ("wrong",)})
    with pytest.raises(AssertionError):
        _assert_producer_parity(context, changed, _workflows()[producer.workflow])


def test_missing_branch_keeps_every_push_producer() -> None:
    assert gate.expected_external_contexts("push") == gate.expected_external_contexts(
        "push", ref_name="main"
    )


def test_queue_preserves_the_existing_repo_evidence_exception() -> None:
    assert gate.expected_external_contexts("merge_group") == tuple(
        name
        for name in EXPECTED_EXTERNAL_CONTEXTS
        if name != "repo-evidence / dod-verify"
    )


@pytest.mark.parametrize("ref_name", ["dev", "main", "hotfix/example"])
def test_pull_request_preserves_the_full_current_tuple(ref_name: str) -> None:
    """The push-branch filter never narrows the pull-request tuple."""

    resolved = gate.expected_external_contexts("pull_request", ref_name=ref_name)
    assert resolved == gate.expected_external_contexts("pull_request")
    assert resolved == EXPECTED_EXTERNAL_CONTEXTS
    # The owning ticket's historical count was 53; the integrated base has 55,
    # and OMN-20287 adds the Deployment Fact Gate.
    assert len(resolved) == 56


@pytest.mark.parametrize("ref_name", [None, "dev", "main"])
@pytest.mark.parametrize("event", [None, "", "repository_dispatch"])
def test_resolver_unknown_events_fail_closed(
    event: str | None, ref_name: str | None
) -> None:
    """A branch never narrows an absent or unknown event's strict set."""

    resolved = gate.expected_external_contexts(event, ref_name=ref_name)
    assert resolved == EXPECTED_EXTERNAL_CONTEXTS


@pytest.mark.parametrize(
    ("branch", "count"), [("main", 21), ("dev", 4), ("hotfix/example", 10)]
)
def test_push_cli_reports_applicable_contexts_and_enforces_them(
    branch: str, count: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*gate.GATE_JOBS, "Tests (Split 1/1)")
    ]
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(jobs))
    contexts = gate.expected_external_contexts("push", ref_name=branch)
    assert len(contexts) == count
    checks = [
        {"name": name, "status": "completed", "conclusion": "success", "id": i}
        for i, name in enumerate(contexts, start=1)
    ]
    checks_file = tmp_path / "checks.json"
    checks_file.write_text(json.dumps(checks))
    args = [
        "--jobs-file",
        str(jobs_file),
        "--check-runs-file",
        str(checks_file),
        "--event",
        "push",
        "--ref-name",
        branch,
    ]
    assert gate.main(args) == gate.EXIT_SUCCESS
    assert f"External contexts asserted: {len(contexts)}" in capsys.readouterr().out
    checks[0]["conclusion"] = "failure"
    checks_file.write_text(json.dumps(checks))
    assert gate.main(args) == gate.EXIT_FAILURE
    assert checks[0]["name"] in capsys.readouterr().out
    checks[0]["conclusion"] = "success"
    absent = checks.pop()["name"]
    checks_file.write_text(json.dumps(checks))
    assert gate.main(args) == gate.EXIT_PENDING
    assert absent in capsys.readouterr().out
    checks_file.write_text("[]")
    assert gate.main(args) == gate.EXIT_PENDING
    checks_file.unlink()
    assert gate.main(args) == gate.EXIT_PENDING
    checks_file.write_text("{")
    with pytest.raises(json.JSONDecodeError):
        gate.main(args)


def test_poller_passes_branch_to_every_gate_invocation() -> None:
    root = Path(__file__).resolve().parents[4]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    step = workflow["jobs"]["ci-summary"]["steps"][-1]
    invocations = [
        line
        for line in step["run"].splitlines()
        if "python3 scripts/ci/ci_summary_gate.py" in line
    ]
    assert len(invocations) == 2
    assert all('--ref-name "${REF_NAME}"' in line for line in invocations)
    assert step["env"]["REF_NAME"] == "${{ github.ref_name }}"


def test_trigger_parity_is_wired_to_required_ci_and_pre_commit() -> None:
    root = Path(__file__).resolve().parents[4]
    workflows = _workflows()
    lint = workflows["ci.yml"]["jobs"]["lint"]
    assert "lint" in gate.STRICT_GATE_JOBS
    step = next(
        step
        for step in lint["steps"]
        if step.get("name") == "CI Summary external producer trigger parity"
    )
    assert "if" not in step
    assert not step.get("continue-on-error")
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "ci-summary-event-scope"
    )
    assert hook["entry"] == step["run"]
    assert hook["stages"] == ["pre-commit"]
    assert hook["pass_filenames"] is False


def test_absent_event_fails_closed() -> None:
    """A caller that forgets the event gets the strict reading, not a skip."""

    assert external_layer_applies(None) is True


def test_unknown_event_fails_closed() -> None:
    """An event nobody considered asserts the layer rather than skipping it.

    This is the direction that matters. A new trigger added to ``ci.yml``
    later must not silently drop L4 enforcement; it must show up as a wedge
    that someone has to reason about.
    """

    assert external_layer_applies("repository_dispatch") is True
    assert external_layer_applies("") is True


def test_pull_request_is_a_merge_admission_event() -> None:
    """Named outright: the gate that admits a merge keeps every context.

    If this ever goes false, the change has stopped being a scoping change
    and has become a weakening of merge admission.
    """

    assert "pull_request" in MERGE_ADMISSION_EVENTS
    assert external_layer_applies("pull_request") is True
    assert len(EXPECTED_EXTERNAL_CONTEXTS) > 0


def test_scoping_is_a_predicate_over_events_not_over_contexts() -> None:
    """No context is removed from the tuple by this change.

    The regression this guards against is a later edit "fixing" a wedge by
    deleting entries instead of scoping the event, which would drop them from
    the pull-request gate too.
    """

    for name in (
        "advisory-job-gate / advisory-job-gate",
        "Hostile Review Gate",
        "pr-title / check-title",
        "deploy-gate / deploy-gate",
    ):
        assert name in EXPECTED_EXTERNAL_CONTEXTS
