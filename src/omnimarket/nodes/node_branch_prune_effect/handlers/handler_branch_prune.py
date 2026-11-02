# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prune merged and closed remote branches under an overlay keep policy (OMN-21890).

A branch is deleted only when its tip equals the head sha of a merged or closed
pull request, so a branch re-created under the same name with a new tip is kept.
Default, protected and open-pull-request branches are never deleted, whatever
the age floor says. Every tip is recorded before its delete; a failed record
write prevents that delete. Dry run is the default and issues no write at all.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
)
from omnimarket.nodes.node_branch_prune_effect.models import (
    EnumBranchAction,
    EnumBranchPruneVerdict,
    EnumPullRequestState,
    ModelBranchDecision,
    ModelBranchInfo,
    ModelBranchPrunePolicy,
    ModelBranchPruneRequest,
    ModelBranchPruneResult,
    ModelPullRequestInfo,
)
from omnimarket.nodes.node_branch_prune_effect.protocols import (
    ProtocolBranchHost,
    ProtocolTipRecord,
)

NODE_NAME = "node_branch_prune_effect"


def load_policy(environ: Mapping[str, str] | None = None) -> ModelBranchPrunePolicy:
    """Read the keep policy from the node overlay, ``<root>/<node>/overlay.yaml``.

    There is no packaged fallback: no overlay, or an invalid one, raises.
    """
    env = os.environ if environ is None else environ
    path = find_node_overlay(NODE_NAME, env)
    if path is None:
        raise NodeOverlayError(
            f"{NODE_NAME} needs its keep policy: put overlay.yaml under "
            f"<root>/{NODE_NAME}/ and list the root in {OVERLAY_ROOTS_ENV}"
        )
    return _read_policy(path)


def _read_policy(path: Path) -> ModelBranchPrunePolicy:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        return ModelBranchPrunePolicy.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise NodeOverlayError(f"overlay {path} is invalid: {exc}") from exc


class HandlerBranchPrune:
    """Decide per branch, then (outside dry run) record the tip and delete."""

    def __init__(
        self,
        *,
        host: ProtocolBranchHost | None = None,
        tips: ProtocolTipRecord | None = None,
        policy: ModelBranchPrunePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._host = host
        self._tips = tips
        self._policy = policy
        self._environ = environ
        self._now = now

    def _injected_host(self) -> ProtocolBranchHost:
        if self._host is None:
            raise RuntimeError("no source-control host injected")
        return self._host

    def _injected_tips(self) -> ProtocolTipRecord:
        if self._tips is None:
            raise RuntimeError("no tips record injected")
        return self._tips

    def handle(
        self, request: ModelBranchPruneRequest | ModelRuntimeTick
    ) -> ModelBranchPruneResult:
        if self._host is None or self._tips is None:
            return ModelBranchPruneResult(
                verdict=EnumBranchPruneVerdict.REFUSED,
                dry_run=True,
                detail=(
                    f"{NODE_NAME} has no source-control host or tips record "
                    "injected; it holds no GitHub client of its own"
                ),
            )
        try:
            policy = self._policy or load_policy(self._environ)
        except NodeOverlayError as exc:
            return ModelBranchPruneResult(
                verdict=EnumBranchPruneVerdict.REFUSED,
                dry_run=True,
                detail=str(exc),
            )
        if isinstance(request, ModelRuntimeTick):
            request = ModelBranchPruneRequest(
                dry_run=policy.scheduled_dry_run, as_of=request.now
            )
        as_of = (request.as_of or self._now or dt.datetime.now(dt.UTC)).astimezone(
            dt.UTC
        )
        decisions: list[ModelBranchDecision] = []
        for repository in policy.repositories:
            decisions.extend(self._decide_repository(repository, policy, as_of))
        if not any(d.action == EnumBranchAction.WOULD_DELETE for d in decisions):
            return ModelBranchPruneResult(
                verdict=EnumBranchPruneVerdict.NOTHING_TO_PRUNE,
                dry_run=request.dry_run,
                decisions=decisions,
            )
        if request.dry_run:
            return ModelBranchPruneResult(
                verdict=EnumBranchPruneVerdict.DRY_RUN,
                dry_run=True,
                decisions=decisions,
            )
        executed = [self._execute(d) for d in decisions]
        ok = all(
            d.action in (EnumBranchAction.DELETED, EnumBranchAction.KEEP)
            for d in executed
        )
        return ModelBranchPruneResult(
            verdict=EnumBranchPruneVerdict.PRUNED
            if ok
            else EnumBranchPruneVerdict.FAILED,
            dry_run=False,
            decisions=executed,
        )

    def _decide_repository(
        self, repository: str, policy: ModelBranchPrunePolicy, as_of: dt.datetime
    ) -> list[ModelBranchDecision]:
        default = self._injected_host().default_branch(repository)
        pulls = self._injected_host().list_pull_requests(repository)
        return [
            self._decide(repository, b, default, pulls, policy, as_of)
            for b in self._injected_host().list_branches(repository)
        ]

    @staticmethod
    def _decide(
        repository: str,
        branch: ModelBranchInfo,
        default: str,
        pulls: list[ModelPullRequestInfo],
        policy: ModelBranchPrunePolicy,
        as_of: dt.datetime,
    ) -> ModelBranchDecision:
        def verdict(action: EnumBranchAction, reason: str) -> ModelBranchDecision:
            return ModelBranchDecision(
                repository=repository,
                branch=branch.name,
                tip_sha=branch.tip_sha,
                action=action,
                reason=reason,
            )

        def keep(reason: str) -> ModelBranchDecision:
            return verdict(EnumBranchAction.KEEP, reason)

        own = [p for p in pulls if p.head_ref == branch.name]
        if branch.name == default:
            return keep("default branch")
        if branch.name in policy.protected_names or any(
            fnmatch.fnmatchcase(branch.name, pat) for pat in policy.protected_patterns
        ):
            return keep("protected by the keep policy")
        if any(p.state == EnumPullRequestState.OPEN for p in own):
            return keep("open pull request")
        age = as_of - branch.tip_committed_at.astimezone(dt.UTC)
        if age < dt.timedelta(days=policy.min_age_days):
            return keep(f"tip younger than the {policy.min_age_days} day age floor")
        for p in own:
            if p.head_sha == branch.tip_sha:
                return verdict(
                    EnumBranchAction.WOULD_DELETE,
                    f"tip equals head of {p.state.value} pull request #{p.number}",
                )
        if own:
            return keep("tip differs from the head of every pull request on this name")
        return keep("no merged or closed pull request")

    def _execute(self, d: ModelBranchDecision) -> ModelBranchDecision:
        if d.action != EnumBranchAction.WOULD_DELETE:
            return d
        try:
            self._injected_tips().record(d.repository, d.branch, d.tip_sha)
        except Exception as exc:  # any failed record write blocks the delete
            return d.model_copy(
                update={
                    "action": EnumBranchAction.RECORD_FAILED,
                    "reason": f"tip record failed, branch kept: {exc}",
                }
            )
        try:
            self._injected_host().delete_branch(
                d.repository, d.branch, expected_sha=d.tip_sha
            )
        except Exception as exc:
            return d.model_copy(
                update={
                    "action": EnumBranchAction.DELETE_FAILED,
                    "reason": f"delete failed: {exc}",
                }
            )
        return d.model_copy(update={"action": EnumBranchAction.DELETED})
