# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_branch_prune_effect (OMN-21890).

One schedule tick on the in-memory bus in, one completed event out, through the handler the
node's contract names, with the source-control host and the tips record faked. The chain is
the scheduled one: a dry run by default (no delete call), and deletes only when the keep
policy turns the dry run off.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_branch_prune_effect.handlers.handler_branch_prune import (
    HandlerBranchPrune,
)
from omnimarket.nodes.node_branch_prune_effect.models import (
    EnumBranchPruneVerdict,
    EnumPullRequestState,
    ModelBranchInfo,
    ModelBranchPrunePolicy,
    ModelBranchPruneResult,
    ModelPullRequestInfo,
)

TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.branch-prune-completed.v1"
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_branch_prune_effect/contract.yaml"
)
_NOW = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
_OLD = _NOW - dt.timedelta(days=60)
_REPO = "acme/widgets"


class _Host:
    def __init__(self) -> None:
        self.deletes: list[tuple[str, str, str]] = []

    def default_branch(self, repository: str) -> str:
        return "main"

    def list_branches(self, repository: str) -> list[ModelBranchInfo]:
        return [
            ModelBranchInfo(name="main", tip_sha="m1", tip_committed_at=_OLD),
            ModelBranchInfo(name="feat/merged", tip_sha="a1", tip_committed_at=_OLD),
            ModelBranchInfo(name="feat/open", tip_sha="c1", tip_committed_at=_OLD),
        ]

    def list_pull_requests(self, repository: str) -> list[ModelPullRequestInfo]:
        return [
            ModelPullRequestInfo(
                number=1,
                head_ref="feat/merged",
                head_sha="a1",
                state=EnumPullRequestState.MERGED,
            ),
            ModelPullRequestInfo(
                number=2,
                head_ref="feat/open",
                head_sha="c1",
                state=EnumPullRequestState.OPEN,
            ),
        ]

    def delete_branch(self, repository: str, branch: str, *, expected_sha: str) -> None:
        self.deletes.append((repository, branch, expected_sha))


class _Tips:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, repository: str, branch: str, tip_sha: str) -> None:
        self.rows.append((repository, branch, tip_sha))


def _tick() -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=_NOW,
        tick_id="00000000-0000-0000-0000-000000000001",
        sequence_number=1,
        scheduled_at=_NOW,
        correlation_id="00000000-0000-0000-0000-000000000002",
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )


def test_the_contract_names_the_handler_and_the_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert getattr(module, contract["handler"]["class"]) is HandlerBranchPrune
    assert contract["event_bus"]["subscribe_topics"] == [TICK_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]


@pytest.mark.parametrize(
    ("scheduled_dry_run", "verdict", "deletes"),
    [
        (True, EnumBranchPruneVerdict.DRY_RUN, 0),
        (False, EnumBranchPruneVerdict.PRUNED, 1),
    ],
)
async def test_a_tick_in_a_completed_event_out(
    scheduled_dry_run: bool, verdict: EnumBranchPruneVerdict, deletes: int
) -> None:
    event_bus = EventBusInmemory()
    host, tips = _Host(), _Tips()
    handler = HandlerBranchPrune(
        host=host,
        tips=tips,
        policy=ModelBranchPrunePolicy(
            repositories=(_REPO,), min_age_days=14, scheduled_dry_run=scheduled_dry_run
        ),
        now=_NOW,
    )
    completed: list[dict[str, Any]] = []

    async def on_completed(message: Any) -> None:
        completed.append(json.loads(message.value))

    async def on_tick(message: Any) -> None:
        result = handler.handle(ModelRuntimeTick(**json.loads(message.value)))
        await event_bus.publish(
            COMPLETED_TOPIC, key=None, value=result.model_dump_json().encode()
        )

    await event_bus.start()
    await event_bus.subscribe(COMPLETED_TOPIC, on_message=on_completed, group_id="done")
    await event_bus.subscribe(TICK_TOPIC, on_message=on_tick, group_id="prune")
    await event_bus.publish(
        TICK_TOPIC, key=None, value=_tick().model_dump_json().encode()
    )

    assert len(completed) == 1
    result = ModelBranchPruneResult(**completed[0])
    assert result.verdict == verdict
    assert len(host.deletes) == deletes
    assert len(tips.rows) == deletes
    assert all(branch == "feat/merged" for _, branch, _ in host.deletes)
    await event_bus.close()
