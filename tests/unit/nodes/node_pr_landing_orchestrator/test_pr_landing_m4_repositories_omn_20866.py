# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing orchestrator's config for every M4 repository (OMN-20866).

Each repository's ``landing_config`` entries come from its own live facts, read
2026-10-10: the dev branch's classic protection and rules (required contexts,
conversation resolution, merge queue) and whether the repository still runs its
change-control autobind caller. AC1 (``-k config``): the contract declares those
entries and the handler builds them. AC2 (``-k enforce``): omnibase_infra, the
one queue repository, replays a recorded green head of its own through the real
classifier and arm gate to READY then an enqueue, dry_run as the contract
declares it and enforce once its mode is flipped; a held PR and a DIRTY PR are
not enqueued; reruns, update-branch and disarm stay dry_run. The three
repositories whose change-control cut-over has not landed keep the companion
step and stay in dry_run.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubCheckRunFact,
    ModelGithubPrStateFact,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingArmMethod,
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingObservedPrompt,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.contract_config import (
    CONFIG_KEY,
    CONTRACT_PATH,
    config_from_block,
    load_contract_config,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    NODE_ID,
    T0,
    answer,
    only_request,
    prompt,
    requests_in,
)

pytestmark = pytest.mark.unit

CANARY = "OmniNode-ai/omnimarket"
INFRA = "OmniNode-ai/omnibase_infra"
CORE = "OmniNode-ai/omnibase_core"
CLAUDE = "OmniNode-ai/omniclaude"
DASH = "OmniNode-ai/omnidash"
# Change-control cut-over not landed: call-occ-autobind.yml on dev and
# occ-preflight / eligibility a required context (read 2026-10-10).
NOT_CUT_OVER = (CORE, CLAUDE, DASH)
_REPLAY = Path(__file__).resolve().parents[3] / "fixtures/pr_landing/head_checks_replay"


def _recorded(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_REPLAY / f"{name}.json").read_text("utf-8"))
    return data


def _contract_block() -> dict[str, Any]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
    block: dict[str, Any] = contract[CONFIG_KEY]
    return block


def _flipped(repository: str) -> PrLandingOrchestratorConfig:
    """The contract's own block with one repository's mode set to enforce."""
    block = _contract_block()
    block["github_mode_by_repository"] = {
        **block["github_mode_by_repository"],
        repository: "enforce",
    }
    return config_from_block(block)


def _handler(
    config: PrLandingOrchestratorConfig | None = None,
) -> HandlerPrLandingOrchestrator:
    """The handler as the runtime builds it: real reducer, gate and classifier."""
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        config=config,
        store=InMemoryPrLandingRowStore(),
    )


def _observed(
    repo: str, pr: int, head: str, *, minutes: float = 0
) -> ModelPrLandingObservedPrompt:
    """A PR watcher payload as it reaches the bus, envelope fields included."""
    at = (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ModelPrLandingObservedPrompt.model_validate(
        {
            "repo": repo.split("/", 1)[1],
            "pr_number": pr,
            "state": "open",
            "head_sha": head,
            "observed_at": at,
            "base": "dev",
            "armed": False,
            "ci_verdict": "GREEN",
            "red_contexts": [],
            "watcher_class": "green-unarmed",
            "schema_version": "1.0.0",
        }
    )


def _pr_state(
    recorded: dict[str, Any], *, labels: tuple[str, ...] = ()
) -> ModelGithubPrStateFact:
    return ModelGithubPrStateFact.model_validate(
        {
            "pr_number": recorded["pr_number"],
            "head_sha": recorded["head_sha"],
            "base_ref": recorded["base_ref"],
            "state": "open",
            "merged": False,
            "draft": False,
            "title": f"feat(OMN-20866): infra pr {recorded['pr_number']}",
            "labels": labels,
            "auto_merge_armed": False,
            "pr_node_id": NODE_ID,
            "mergeable_state": recorded["merge_state_status"].lower(),
        }
    )


def _head_checks_answer(
    request: ModelPrLandingGithubRequest, recorded: dict[str, Any]
) -> ModelPrLandingGithubCompletedIngress:
    base = answer(
        request,
        check_runs=ModelGithubCheckRunFact.from_check_runs_body(
            recorded["check_runs_body"]
        ),
    )
    return base.model_copy(
        update={"required_contexts": tuple(recorded["required_contexts"])}
    )


def _states(emitted: list[BaseModel]) -> list[EnumPrLandingState]:
    return [e.to_state for e in emitted if isinstance(e, ModelPrLandingTransitioned)]


async def _infra_to_head_check_read(
    handler: HandlerPrLandingOrchestrator,
    recorded: dict[str, Any],
    *,
    labels: tuple[str, ...] = (),
) -> list[BaseModel]:
    """Watcher prompt, PR read answered; return what the read's answer emitted."""
    pr, head = recorded["pr_number"], recorded["head_sha"]
    read = only_request(await handler.handle(_observed(INFRA, pr, head)))
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    return await handler.handle(
        answer(read, pr_state=_pr_state(recorded, labels=labels))
    )


# ------------------------------------------------------------------- AC1 config


def test_config_each_m4_repository_carries_its_own_facts() -> None:
    config = load_contract_config()
    # Mode: only the canary enforces; omnibase_infra waits on the lab gate and
    # the three repositories that are not cut over keep the companion step.
    assert config.github_mode_for(CANARY) is EnumPrLandingGithubMode.ENFORCE
    for repository in (INFRA, *NOT_CUT_OVER):
        assert config.github_mode_for(repository) is EnumPrLandingGithubMode.DRY_RUN
    # omnibase_infra removed every change-control caller; the other three did not.
    assert not config.companion_required(INFRA)
    for repository in NOT_CUT_OVER:
        assert config.companion_required(repository)
    # omnibase_infra's dev branch has a merge-queue rule; no other M4 dev does.
    assert config.queue_repos == {INFRA}
    assert config.arm_method(INFRA) is EnumPrLandingArmMethod.QUEUE
    for repository in (CANARY, *NOT_CUT_OVER):
        assert config.arm_method(repository) is EnumPrLandingArmMethod.AUTO_MERGE
    # No M4 dev branch requires conversation resolution.
    assert config.review_threads_not_required_repos == {
        CANARY,
        INFRA,
        *NOT_CUT_OVER,
    }
    # omnibase_infra publishes no autobind prompt, so the watcher prompts it;
    # the three that still publish one are prompted by it.
    assert config.observed_prompt_repos == {CANARY, INFRA}
    assert config.dry_run_operations == {
        EnumPrLandingGithubOperation.RERUN_RUNS,
        EnumPrLandingGithubOperation.UPDATE_BRANCH,
        EnumPrLandingGithubOperation.DISARM,
    }


@pytest.mark.parametrize("repository", NOT_CUT_OVER)
async def test_config_a_repository_not_cut_over_sends_its_companion_dry_run(
    repository: str,
) -> None:
    """Its push prompt still starts the companion step, and it stays a dry run."""
    handler = _handler()
    read = only_request(await handler.handle(prompt(repo=repository)))
    recorded = {
        "pr_number": 4242,
        "head_sha": "1" * 40,
        "base_ref": "dev",
        "merge_state_status": "CLEAN",
    }
    emitted = await handler.handle(answer(read, pr_state=_pr_state(recorded)))
    assert _states(emitted)[-1] is EnumPrLandingState.COMPANION_PENDING
    (command,) = [e for e in emitted if isinstance(e, ModelPrLifecycleFixCommand)]
    assert command.repo == repository
    assert command.dry_run is True
    assert requests_in(emitted) == []


@pytest.mark.parametrize("repository", NOT_CUT_OVER)
async def test_config_a_watcher_observation_of_a_repository_not_cut_over_is_dropped(
    repository: str,
) -> None:
    assert await _handler().handle(_observed(repository, 7, "1" * 40)) == []


# ------------------------------------------------------------------ AC2 enforce


async def test_enforce_infra_green_head_is_enqueued_dry_run_as_the_contract_declares() -> (
    None
):
    """omnibase_infra#4807, recorded green and CLEAN: READY, then a dry_run enqueue."""
    handler = _handler()
    recorded = _recorded("omnibase_infra_4807")
    emitted = await _infra_to_head_check_read(handler, recorded)
    # Companion-exempt: no companion command, straight to the head-check read.
    assert [e for e in emitted if isinstance(e, ModelPrLifecycleFixCommand)] == []
    head_read = only_request(emitted)
    assert head_read.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
    assert head_read.base_ref == "dev"
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert _states(emitted) == [EnumPrLandingState.READY]
    enqueue = only_request(emitted)
    assert enqueue.operation is EnumPrLandingGithubOperation.ENQUEUE
    assert enqueue.mode is EnumPrLandingGithubMode.DRY_RUN


async def test_enforce_infra_green_head_reaches_ready_then_armed_once_flipped() -> None:
    """The queue path the flip turns on: READY, an enforce enqueue, then ARMED."""
    handler = _handler(_flipped(INFRA))
    recorded = _recorded("omnibase_infra_4807")
    head_read = only_request(await _infra_to_head_check_read(handler, recorded))
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert _states(emitted) == [EnumPrLandingState.READY]
    enqueue = only_request(emitted)
    assert enqueue.operation is EnumPrLandingGithubOperation.ENQUEUE
    assert enqueue.mode is EnumPrLandingGithubMode.ENFORCE
    assert enqueue.head_sha == recorded["head_sha"]
    assert enqueue.pr_node_id == NODE_ID
    emitted = await handler.handle(answer(enqueue))
    assert _states(emitted) == [EnumPrLandingState.ARMED]


async def test_enforce_infra_dirty_head_is_not_enqueued() -> None:
    """omnibase_infra#4691, recorded green but DIRTY: the arm gate withholds."""
    handler = _handler(_flipped(INFRA))
    recorded = _recorded("omnibase_infra_4691")
    head_read = only_request(await _infra_to_head_check_read(handler, recorded))
    emitted = await handler.handle(_head_checks_answer(head_read, recorded))
    assert EnumPrLandingState.ARMED not in _states(emitted)
    assert requests_in(emitted) == []


async def test_enforce_infra_held_pr_is_not_enqueued() -> None:
    handler = _handler(_flipped(INFRA))
    recorded = _recorded("omnibase_infra_4807")
    emitted = await _infra_to_head_check_read(
        handler, recorded, labels=("do-not-merge",)
    )
    assert _states(emitted)[-1] is EnumPrLandingState.PARKED
    assert requests_in(emitted) == []


@pytest.mark.parametrize(
    "operation",
    [
        EnumPrLandingGithubOperation.RERUN_RUNS,
        EnumPrLandingGithubOperation.UPDATE_BRANCH,
        EnumPrLandingGithubOperation.DISARM,
    ],
)
@pytest.mark.parametrize("repository", [INFRA, *NOT_CUT_OVER])
def test_enforce_reruns_update_branch_and_disarm_stay_dry_run_after_a_flip(
    repository: str, operation: EnumPrLandingGithubOperation
) -> None:
    config = _flipped(repository)
    assert config.github_mode_for(repository) is EnumPrLandingGithubMode.ENFORCE
    assert (
        config.mutation_mode(repository, operation) is EnumPrLandingGithubMode.DRY_RUN
    )
