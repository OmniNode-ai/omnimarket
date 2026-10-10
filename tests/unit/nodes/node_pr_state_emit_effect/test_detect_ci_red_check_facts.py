# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ci-run-failed names run id, workflow and completed_at, from either source of the red."""

from typing import Any

import pytest

from omnimarket.events.pr_state import EnumPrState, ModelPrStateObservedEvent
from omnimarket.models.ci_red_triage import ModelCiRunFailedEvent, facts_from_event
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    CiRedIndex,
    HandlerDetectCiRed,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red_check_run import (
    HandlerDetectCiRedCheckRun,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_record_workflow_run import (
    HandlerRecordWorkflowRun,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import (
    event,
    event_v2,
)

pytestmark = pytest.mark.unit

REPO = "omnimarket"
FULL = "OmniNode-ai/omnimarket"
HEAD = "a" * 40


def wire(e: ModelPrStateObservedEvent) -> dict[str, Any]:
    return {**e.model_dump(mode="json"), "actor": "watcher", "lane": "dev"}


def check_run(check: str = "unit", **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic": "onex.evt.github.check-run.v1",
        "repo": FULL,
        "pr_number": 3050,
        "delivery_id": "6a1b7a3e-3f5d-4c1c-9d8e-5d0f6c2a1b11",
        "github_event": "check_run",
        "as_of": "2026-10-08T10:00:30Z",
        "head_sha": HEAD,
        "base_ref": "dev",
        "check": check,
        "status": "completed",
        "conclusion": "failure",
        "run_id": 17000000001,
        "completed_at": "2026-10-08T10:00:30Z",
    }
    payload.update(changes)
    return payload


def workflow_run(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo": FULL,
        "run_id": 17000000001,
        "workflow": "CI",
        "head_sha": HEAD,
        "status": "in_progress",
    }
    payload.update(changes)
    return payload


def v1_red(**changes: Any) -> ModelPrStateObservedEvent:
    return event(
        repo=REPO,
        ci_verdict="RED",
        red_contexts=("unit",),
        armed=True,
        **changes,
    )


@pytest.mark.asyncio
async def test_v2_observation_gives_the_event_every_failing_checks_facts() -> None:
    watcher = HandlerDetectCiRed(index=CiRedIndex())
    out = await watcher.handle(
        wire(
            event_v2(
                repo=REPO,
                armed=True,
                base_red_checks=("lint",),
            )
        )
    )
    [red] = out.events
    assert isinstance(red, ModelCiRunFailedEvent)
    assert red.failing_checks == ("CI Summary", "unit")
    assert {r.check for r in red.failing_runs} == {"CI Summary", "unit"}
    assert all(r.run_id and r.workflow and r.completed_at for r in red.failing_runs)
    assert red.base_read is True
    assert red.base_red_checks == ("lint",)
    facts = facts_from_event(red)
    assert facts is not None
    assert facts.check_conclusions == {"CI Summary": "failure", "unit": "timed_out"}


@pytest.mark.asyncio
async def test_v1_observation_still_emits_without_facts() -> None:
    watcher = HandlerDetectCiRed(index=CiRedIndex())
    [red] = (await watcher.handle(wire(v1_red()))).events
    assert red.failing_runs == ()
    assert red.base_read is False
    assert facts_from_event(red) is None


@pytest.mark.asyncio
async def test_check_run_conclusion_emits_before_the_watcher_sees_the_red() -> None:
    index = CiRedIndex()
    watcher = HandlerDetectCiRed(index=index)
    checker = HandlerDetectCiRedCheckRun(index=index)
    # The watcher knows the PR at a green head; GitHub then reports a red check on it.
    await watcher.handle(
        wire(event(repo=REPO, armed=True, observed_at="2026-10-08T10:00:00Z"))
    )
    await HandlerRecordWorkflowRun(index=index).handle(workflow_run())
    [red] = (await checker.handle(check_run())).events
    assert red.failing_checks == ("unit",)
    assert red.armed is True
    assert red.repo == REPO
    assert red.head_sha == HEAD
    [run] = red.failing_runs
    assert (run.check, run.conclusion, run.run_id, run.workflow, run.completed_at) == (
        "unit",
        "failure",
        17000000001,
        "CI",
        "2026-10-08T10:00:30Z",
    )
    assert red.observed_at == red.ci_read_at == "2026-10-08T10:00:30Z"


@pytest.mark.asyncio
async def test_a_second_red_check_extends_the_set_and_a_repeat_is_silent() -> None:
    index = CiRedIndex()
    await HandlerDetectCiRed(index=index).handle(wire(event(repo=REPO, armed=True)))
    await HandlerRecordWorkflowRun(index=index).handle(workflow_run())
    checker = HandlerDetectCiRedCheckRun(index=index)
    assert len((await checker.handle(check_run("unit"))).events) == 1
    assert (await checker.handle(check_run("unit"))).events == ()
    [second] = (await checker.handle(check_run("lint"))).events
    assert second.failing_checks == ("lint", "unit")
    assert {r.check for r in second.failing_runs} == {"lint", "unit"}
    assert second.carries_check_facts


@pytest.mark.asyncio
async def test_watcher_observation_of_the_same_red_does_not_emit_twice() -> None:
    index = CiRedIndex()
    watcher = HandlerDetectCiRed(index=index)
    await watcher.handle(wire(event(repo=REPO, armed=True)))
    await HandlerRecordWorkflowRun(index=index).handle(workflow_run())
    checker = HandlerDetectCiRedCheckRun(index=index)
    assert len((await checker.handle(check_run("unit"))).events) == 1
    later = v1_red(observed_at="2026-10-08T10:05:00Z")
    assert (await watcher.handle(wire(later))).events == ()


@pytest.mark.asyncio
async def test_peers_come_from_the_shared_index() -> None:
    index = CiRedIndex()
    watcher = HandlerDetectCiRed(index=index)
    await watcher.handle(wire(v1_red(pr_number=1, head_sha="b" * 40)))
    await watcher.handle(wire(event(repo=REPO, armed=True)))
    await HandlerRecordWorkflowRun(index=index).handle(workflow_run())
    [red] = (
        await HandlerDetectCiRedCheckRun(index=index).handle(check_run("unit"))
    ).events
    assert [(p.pr_number, p.armed) for p in red.peers] == [(1, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior", "delivery"),
    [
        pytest.param("none", {}, id="pr-unknown-to-the-index"),
        pytest.param("closed", {}, id="pr-closed"),
        pytest.param("draft", {}, id="pr-draft"),
        pytest.param("open", {"conclusion": "success"}, id="passing"),
        pytest.param("open", {"conclusion": "cancelled"}, id="no-verdict"),
        pytest.param("open", {"run_id": 17000000777}, id="workflow-not-learned-yet"),
    ],
)
async def test_check_run_that_cannot_decide_emits_nothing(
    prior: str, delivery: dict[str, Any]
) -> None:
    index = CiRedIndex()
    if prior == "open":
        await HandlerDetectCiRed(index=index).handle(wire(event(repo=REPO, armed=True)))
    elif prior == "closed":
        await HandlerDetectCiRed(index=index).handle(
            wire(event(repo=REPO, state=EnumPrState.CLOSED))
        )
    elif prior == "draft":
        await HandlerDetectCiRed(index=index).handle(wire(event(repo=REPO, draft=True)))
    await HandlerRecordWorkflowRun(index=index).handle(workflow_run())
    out = await HandlerDetectCiRedCheckRun(index=index).handle(check_run(**delivery))
    assert out.events == ()


@pytest.mark.asyncio
async def test_a_check_posted_by_another_app_has_no_run_or_workflow() -> None:
    index = CiRedIndex()
    await HandlerDetectCiRed(index=index).handle(wire(event(repo=REPO, armed=True)))
    [red] = (
        await HandlerDetectCiRedCheckRun(index=index).handle(
            check_run("CodeRabbit", run_id=None)
        )
    ).events
    [run] = red.failing_runs
    assert (run.run_id, run.workflow) == (0, "")


@pytest.mark.asyncio
async def test_workflow_names_also_come_from_version_2_watcher_facts() -> None:
    index = CiRedIndex()
    await HandlerDetectCiRed(index=index).handle(
        wire(event_v2(repo=REPO, armed=True, observed_at="2026-10-08T10:00:00Z"))
    )
    [red] = (
        await HandlerDetectCiRedCheckRun(index=index).handle(
            check_run("other", run_id=17000000002, head_sha="c" * 40)
        )
    ).events
    assert red.failing_checks == ("other",)
    assert red.failing_runs[0].workflow == "CI"


@pytest.mark.asyncio
async def test_malformed_check_run_is_refused_at_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await HandlerDetectCiRedCheckRun(index=CiRedIndex()).handle(
            check_run(completed_at="yesterday")
        )
    with pytest.raises(ValidationError):
        await HandlerRecordWorkflowRun(index=CiRedIndex()).handle(
            workflow_run(status="queued")
        )
