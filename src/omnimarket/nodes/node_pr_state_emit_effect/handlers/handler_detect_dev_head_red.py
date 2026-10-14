# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit a red landing-branch head the moment GitHub reports its verdict (OMN-20730).

A dev push run that goes red names no pull request, so the ci-run-failed path never carries it.
node_github_webhook_ingress_effect folds the summary check run of a watched branch into a
``branch-head-status`` observation; this handler turns a red one on a landing branch into one
``dev-head-red`` event. A merge-group verdict, a ref advance, a green, pending or cancelled
verdict and a branch that is not a landing branch emit nothing.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.topics import DEV_HEAD_RED_TOPIC_V1, GITHUB_BRANCH_HEAD_TOPIC_V1
from omnimarket.models.dev_head_red import (
    ModelDevHeadRedEvent,
    dev_head_red_event_id,
)
from omnimarket.models.github_check_run import (
    RED_CHECK_CONCLUSIONS,
    observation_from_wire,
)

MEMORY_LIMIT = 4096
# Event ids this process emitted. It only saves work: the dispatcher decides each red once.
DEV_HEAD_RED_INDEX: OrderedDict[str, None] = OrderedDict()


class ModelBranchHeadVerdict(BaseModel):
    """The consuming view of node_github_webhook_ingress_effect's branch-head observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["branch-ref-advanced", "branch-head-status", "merge-group-status"]
    repo: str = Field(min_length=3)
    branch: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    as_of: datetime
    sha: str = Field(min_length=1)
    ci_status: str | None = None
    check_name: str | None = None
    merge_group_ref: str | None = None


class ModelDetectDevHeadRedRequest(BaseModel):
    """Validate the branch-head wire payload before runtime handler dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation: ModelBranchHeadVerdict

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "observation" not in value:
            return {"observation": observation_from_wire(ModelBranchHeadVerdict, value)}
        return value


def _landing_branches() -> frozenset[str]:
    contract = Path(__file__).resolve().parents[1] / "contract.yaml"
    with contract.open() as stream:
        branches = yaml.safe_load(stream)["dev_head_red"]["branches"]
    if not isinstance(branches, list) or not all(
        isinstance(b, str) and b for b in branches
    ):
        raise ValueError("dev_head_red.branches must list branch names")
    return frozenset(branches)


def _iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("branch-head as_of has no timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerDetectDevHeadRed:
    """One dev-head-red event per red landing-branch head and check."""

    MEMORY_LIMIT = MEMORY_LIMIT
    subscribe_topic = GITHUB_BRANCH_HEAD_TOPIC_V1
    published_event_topics = {ModelDevHeadRedEvent: DEV_HEAD_RED_TOPIC_V1}

    def __init__(
        self,
        *,
        branches: frozenset[str] | None = None,
        index: OrderedDict[str, None] | None = None,
    ) -> None:
        self._branches = branches if branches is not None else _landing_branches()
        self._index = index if index is not None else DEV_HEAD_RED_INDEX

    def _red(self, verdict: ModelBranchHeadVerdict) -> ModelDevHeadRedEvent | None:
        conclusion = (verdict.ci_status or "").lower()
        if (
            verdict.kind != "branch-head-status"
            or verdict.merge_group_ref is not None
            or verdict.branch not in self._branches
            or conclusion not in RED_CHECK_CONCLUSIONS
            or not verdict.check_name
        ):
            return None
        return ModelDevHeadRedEvent(
            event_id=dev_head_red_event_id(
                verdict.repo, verdict.branch, verdict.sha, verdict.check_name
            ),
            repo=verdict.repo,
            branch=verdict.branch,
            head_sha=verdict.sha,
            check=verdict.check_name,
            conclusion=conclusion,
            completed_at=_iso_z(verdict.as_of),
            source_delivery=verdict.delivery_id,
        )

    async def handle(
        self, request: ModelDetectDevHeadRedRequest | Mapping[str, object]
    ) -> ModelHandlerOutput[None]:
        verdict = (
            request
            if isinstance(request, ModelDetectDevHeadRedRequest)
            else ModelDetectDevHeadRedRequest.model_validate(request)
        ).observation
        event = self._red(verdict)
        events: tuple[ModelDevHeadRedEvent, ...] = ()
        if event is not None and event.event_id not in self._index:
            self._index[event.event_id] = None
            while len(self._index) > self.MEMORY_LIMIT:
                self._index.popitem(last=False)
            events = (event,)
        elif event is not None:
            self._index.move_to_end(event.event_id)
        identity = (
            event.event_id
            if event is not None
            else f"{verdict.delivery_id}:{verdict.repo}@{verdict.branch}:{verdict.sha}"
        )
        correlation_id = uuid5(NAMESPACE_URL, "onex:dev-head-red:" + identity)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_state_emit_effect.detect_dev_head_red",
            events=events,
        )
