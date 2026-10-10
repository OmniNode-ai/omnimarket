# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC-M9 (OMN-20750): a green, armed PR that goes DIRTY gets one conflict request.

The replay is omnimarket#3722 on 2026-10-10: head d004fc6c0 was green and armed
on a dev branch it merged cleanly into, then #3720 and #3719 landed on dev and
the PR conflicted with it in contracts/OMN-20867.yaml. GitHub's read then
reports ``mergeable_state`` dirty with auto-merge still armed. The watcher's
observation prompts the orchestrator's own PR read, so the merge state and the
base head come from that read.

The orchestrator answers a DIRTY, armed PR with exactly one conflict command
(node_pr_lifecycle_fix_effect's ``conflict`` block reason on its fix-start
topic) per (PR, head, base head), dry_run like every update-branch, and never
with an arm or an enqueue. A moved head or base allows a new request; a held PR
gets none.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubOperation,
    ModelGithubPrStateFact,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingObservedPrompt,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    NODE_ID,
    T0,
    FixedClassifier,
    answer,
    only_request,
    requests_in,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
PR = 3722
HEAD = "d004fc6c0ca6499e75b247711565caef5ee3bd4f"
HEAD_PUSHED = "e" * 40
# dev when #3722 was green and armed (#3714), then after #3720 and #3719 landed,
# then after #3710 landed: the head conflicts with the last two.
BASE_GREEN = "c68ace7092f9a40989549b40e43794c90311ef75"
BASE_DIRTY = "1b1041574e98645068fab28d6f5ca4fd9328674b"
BASE_MOVED = "776174028a9cea1c33d490ff70f21d9115ea6628"
TITLE = "feat(OMN-20867): merge-sweep plan runs from the runtime tick"
_ARM_OPERATIONS = {
    EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
    EnumPrLandingGithubOperation.ENQUEUE,
}


def _handler() -> HandlerPrLandingOrchestrator:
    """The runtime's handler: contract config, real reducer and arm gate."""
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        classifier=FixedClassifier(),
        store=InMemoryPrLandingRowStore(),
    )


def _observed(minutes: float, head: str = HEAD) -> ModelPrLandingObservedPrompt:
    """One pr-state-observed record for #3722, green and armed, as on the bus."""
    at = (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ModelPrLandingObservedPrompt.model_validate(
        {
            "repo": "omnimarket",
            "pr_number": PR,
            "state": "open",
            "head_sha": head,
            "observed_at": at,
            "base": "dev",
            "armed": True,
            "ci_verdict": "GREEN",
            "red_contexts": [],
            "watcher_class": "green-armed",
            "schema_version": "1.0.0",
        }
    )


def _fact(
    *,
    base_sha: str,
    mergeable_state: str,
    head: str = HEAD,
    labels: tuple[str, ...] = (),
) -> ModelGithubPrStateFact:
    """GitHub's REST read of #3722: open, armed, with its base head and merge state."""
    return ModelGithubPrStateFact.model_validate(
        {
            "pr_number": PR,
            "head_sha": head,
            "base_ref": "dev",
            "base_sha": base_sha,
            "state": "open",
            "merged": False,
            "draft": False,
            "title": TITLE,
            "labels": labels,
            "auto_merge_armed": True,
            "auto_merge_method": "SQUASH",
            "pr_node_id": NODE_ID,
            "mergeable_state": mergeable_state,
        }
    )


def _conflict_requests(emitted: list[BaseModel]) -> list[ModelPrLifecycleFixCommand]:
    return [
        e
        for e in emitted
        if isinstance(e, ModelPrLifecycleFixCommand)
        and e.block_reason is EnumPrBlockReason.CONFLICT
    ]


def _arms(emitted: list[BaseModel]) -> list[EnumPrLandingGithubOperation]:
    return [r.operation for r in requests_in(emitted) if r.operation in _ARM_OPERATIONS]


def _states(emitted: list[BaseModel]) -> list[EnumPrLandingState]:
    return [e.to_state for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


async def _read(
    handler: HandlerPrLandingOrchestrator,
    minutes: float,
    fact: ModelGithubPrStateFact,
) -> list[BaseModel]:
    """A watcher record prompts a read; return what the read's answer emitted."""
    read = only_request(await handler.handle(_observed(minutes, fact.head_sha)))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    return await handler.handle(answer(read, pr_state=fact))


async def _green_and_armed(handler: HandlerPrLandingOrchestrator) -> None:
    """#3722 at BASE_GREEN: read clean, head checks green, armed, ARMED."""
    emitted = await _read(
        handler, 0, _fact(base_sha=BASE_GREEN, mergeable_state="clean")
    )
    assert _conflict_requests(emitted) == []
    head_read = only_request(emitted)
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    emitted = await handler.handle(answer(head_read))
    assert _states(emitted) == [EnumPrLandingState.READY]
    arm = only_request(emitted)
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    emitted = await handler.handle(answer(arm))
    assert _states(emitted) == [EnumPrLandingState.ARMED]
    assert _conflict_requests(emitted) == []


def test_the_rest_read_carries_the_base_head_and_the_merge_state() -> None:
    """The key's base head comes from GitHub's own read of the pull request."""
    body: dict[str, object] = {
        "node_id": NODE_ID,
        "number": PR,
        "head": {"sha": HEAD},
        "base": {"ref": "dev", "sha": BASE_DIRTY},
        "state": "open",
        "draft": False,
        "title": TITLE,
        "labels": [],
        "merged": False,
        "auto_merge": {"merge_method": "squash"},
        "mergeable_state": "dirty",
    }
    fact = ModelGithubPrStateFact.from_rest_pull(body)
    assert fact.base_sha == BASE_DIRTY
    assert fact.mergeable_state == "dirty"
    assert fact.auto_merge_armed is True
    body["base"] = {"ref": "dev"}
    assert ModelGithubPrStateFact.from_rest_pull(body).base_sha is None


async def test_a_green_armed_pr_gone_dirty_gets_one_conflict_request() -> None:
    handler = _handler()
    await _green_and_armed(handler)
    emitted = await _read(
        handler, 5, _fact(base_sha=BASE_DIRTY, mergeable_state="dirty")
    )
    (request,) = _conflict_requests(emitted)
    assert request.repo == REPO
    assert request.pr_number == PR
    assert request.ticket_id == "OMN-20867"
    # The controller still owns conflict work (one owner per class): the
    # request stays dry_run, as every update-branch does, in every repository.
    assert request.dry_run is True
    assert _arms(emitted) == []


async def test_the_same_pr_head_and_base_never_get_a_second_request() -> None:
    handler = _handler()
    await _green_and_armed(handler)
    dirty = _fact(base_sha=BASE_DIRTY, mergeable_state="dirty")
    assert len(_conflict_requests(await _read(handler, 5, dirty))) == 1
    for minutes in (10, 15):
        emitted = await _read(handler, minutes, dirty)
        assert _conflict_requests(emitted) == []
        assert _arms(emitted) == []


async def test_a_moved_base_allows_a_new_request() -> None:
    handler = _handler()
    await _green_and_armed(handler)
    first = _conflict_requests(
        await _read(handler, 5, _fact(base_sha=BASE_DIRTY, mergeable_state="dirty"))
    )
    emitted = await _read(
        handler, 10, _fact(base_sha=BASE_MOVED, mergeable_state="dirty")
    )
    second = _conflict_requests(emitted)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id != second[0].correlation_id
    assert _arms(emitted) == []


async def test_a_moved_head_allows_a_new_request() -> None:
    handler = _handler()
    await _green_and_armed(handler)
    assert (
        len(
            _conflict_requests(
                await _read(
                    handler, 5, _fact(base_sha=BASE_DIRTY, mergeable_state="dirty")
                )
            )
        )
        == 1
    )
    emitted = await _read(
        handler,
        10,
        _fact(base_sha=BASE_DIRTY, mergeable_state="dirty", head=HEAD_PUSHED),
    )
    assert len(_conflict_requests(emitted)) == 1
    assert _arms(emitted) == []


async def test_a_held_pr_gone_dirty_gets_no_request() -> None:
    handler = _handler()
    await _green_and_armed(handler)
    emitted = await _read(
        handler,
        5,
        _fact(base_sha=BASE_DIRTY, mergeable_state="dirty", labels=("do-not-merge",)),
    )
    assert _conflict_requests(emitted) == []
    assert _arms(emitted) == []
