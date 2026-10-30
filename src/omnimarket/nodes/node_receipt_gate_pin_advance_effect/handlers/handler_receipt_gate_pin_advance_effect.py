# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReceiptGatePinAdvanceEffect - open one pin pull request per stale caller (OMN-20134).

Triggered by the source repository's dev-merge tag event. It reads the source
repository's dev tags with the gate file's blob id at each, reads every
declared caller's pin-site files at its base head, has
node_receipt_gate_pin_advance_compute plan the advance, and for each caller the
plan edits: publishes one branch whose single commit holds the pin line and its
test constant, then opens the pull request through node_github_repo_write_effect.

Idempotent per (caller, target tag): an existing branch is reused and the write
node returns an open pull request for that head replayed. A caller that is
current gets nothing. A caller the planner could not judge is reported in
``unjudged`` and gets no pull request. A port that cannot answer raises, so the
runtime dead-letters the trigger instead of advancing half the callers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from pydantic import SecretStr

from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.handlers.handler_receipt_gate_pin_advance import (
    HandlerReceiptGatePinAdvance,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    ModelCallerPlan,
    ModelReceiptGatePinAdvanceRequest,
    ModelReceiptGatePinPolicy,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.git_core_tag_source import (
    GitCloneCoreTagSource,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.github_pin_repository import (
    GithubPinRepository,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.scm_bus_pr_opener import (
    ScmWritePrOpener,
    bus_write_call,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.models import (
    EnumPinAdvanceDisposition,
    EnumPinAdvanceMode,
    EnumPinPrOutcome,
    ModelDevMergeTagEvent,
    ModelPinAdvanceEffectConfig,
    ModelPinPrResult,
    ModelPinUnjudged,
    ModelReceiptGatePinAdvanceCompleted,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.policy_source import (
    load_pin_policy,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.protocols import (
    PinAdvanceError,
    ProtocolCallerRepository,
    ProtocolCoreTagSource,
    ProtocolPinPrOpener,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_CONFIG_KEY = "pin_advance_effect_config"


def load_effect_config(
    contract_path: Path = _CONTRACT_PATH,
) -> ModelPinAdvanceEffectConfig:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelPinAdvanceEffectConfig.model_validate(contract[_CONFIG_KEY])


class HandlerReceiptGatePinAdvanceEffect:
    """EFFECT handler: plan, publish one branch per stale caller, open its pull request."""

    def __init__(
        self,
        *,
        planner: HandlerReceiptGatePinAdvance | None = None,
        policy: ModelReceiptGatePinPolicy | None = None,
        config: ModelPinAdvanceEffectConfig | None = None,
        tag_source: ProtocolCoreTagSource | None = None,
        repository: ProtocolCallerRepository | None = None,
        pr_opener: ProtocolPinPrOpener | None = None,
    ) -> None:
        self._planner = (
            planner if planner is not None else HandlerReceiptGatePinAdvance()
        )
        self._policy = policy if policy is not None else load_pin_policy()
        self._config = config if config is not None else load_effect_config()
        self._tag_source = tag_source
        self._repository = repository
        self._pr_opener = pr_opener

    async def handle(
        self, event: ModelDevMergeTagEvent
    ) -> ModelReceiptGatePinAdvanceCompleted:
        policy = self._policy
        if event.repo != policy.source_repository:
            return ModelReceiptGatePinAdvanceCompleted(
                trigger_tag=event.tag,
                disposition=EnumPinAdvanceDisposition.IGNORED,
                detail=f"{event.repo} is not the source repository "
                f"{policy.source_repository}",
            )
        tag_source = self._tag_source or self._default_tag_source()
        repository = self._repository or await self._default_repository()
        tags = await asyncio.to_thread(tag_source.dev_tags, required_tag=event.tag)
        snapshots = []
        for caller in policy.callers:
            snapshots.append(await asyncio.to_thread(repository.read_snapshot, caller))
        planned = self._planner.handle(
            ModelReceiptGatePinAdvanceRequest(
                policy=policy, core_tags=tags, callers=tuple(snapshots)
            )
        )
        dry_run = event.dry_run or self._config.mode is EnumPinAdvanceMode.DRY_RUN
        opener = None if dry_run else (self._pr_opener or self._default_pr_opener())
        correlation_id = event.delivery_id or uuid4()
        results = []
        for plan in planned.advances:
            results.append(
                await asyncio.to_thread(
                    self._advance, plan, repository, opener, event, correlation_id
                )
            )
        unjudged = tuple(
            ModelPinUnjudged(
                repository=p.repository, verdict=p.verdict, detail=p.detail
            )
            for p in planned.violations
            if not p.needs_pull_request
        )
        return ModelReceiptGatePinAdvanceCompleted(
            trigger_tag=event.tag,
            disposition=(
                EnumPinAdvanceDisposition.ADVANCED
                if results
                else EnumPinAdvanceDisposition.NOTHING_STALE
            ),
            newest_tag=planned.newest_tag,
            results=tuple(results),
            unjudged=unjudged,
        )

    @staticmethod
    def _advance(
        plan: ModelCallerPlan,
        repository: ProtocolCallerRepository,
        opener: ProtocolPinPrOpener | None,
        event: ModelDevMergeTagEvent,
        correlation_id: UUID,
    ) -> ModelPinPrResult:
        assert plan.target_tag is not None  # a plan with edits always has a target

        def result(
            outcome: EnumPinPrOutcome,
            *,
            detail: str = "",
            branch_head_sha: str | None = None,
            pr_number: int | None = None,
            pr_url: str | None = None,
        ) -> ModelPinPrResult:
            return ModelPinPrResult(
                repository=plan.repository,
                verdict=plan.verdict,
                outcome=outcome,
                target_tag=plan.target_tag or "",
                branch=plan.branch,
                branch_head_sha=branch_head_sha,
                pr_number=pr_number,
                pr_url=pr_url,
                detail=detail,
            )

        if opener is None:
            return result(
                EnumPinPrOutcome.DRY_RUN,
                detail="dry_run: no branch published, no pull request opened",
            )
        try:
            head = repository.publish_branch(
                repository=plan.repository,
                base_head_sha=plan.base_head_sha,
                branch=plan.branch,
                message=plan.commit_message,
                edits=plan.edits,
            )
            replayed, number, url = opener.open(
                plan,
                branch_head_sha=head,
                requested_by_lane=event.requested_by_lane,
                correlation_id=correlation_id,
            )
        except PinAdvanceError as exc:
            return result(EnumPinPrOutcome.FAILED, detail=str(exc))
        return result(
            EnumPinPrOutcome.REPLAYED if replayed else EnumPinPrOutcome.OPENED,
            branch_head_sha=head,
            pr_number=number,
            pr_url=url,
        )

    def _default_tag_source(self) -> ProtocolCoreTagSource:
        root = os.environ.get(self._config.source_clone_root_env)
        if not root:
            raise PinAdvanceError(
                f"{self._config.source_clone_root_env} is not set; it names the "
                "directory that holds the source repository's clone"
            )
        policy = self._policy
        return GitCloneCoreTagSource(
            Path(root) / policy.source_repository.split("/")[1],
            policy.workflow_path,
            fetch_tags=self._config.fetch_tags,
        )

    async def _default_repository(self) -> ProtocolCallerRepository:
        secret: SecretStr | None = await resolve_api_key_async(
            contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        )
        if secret is None:
            raise PinAdvanceError(
                "GITHUB_TOKEN resolved to nothing in the secret store"
            )
        return GithubPinRepository(UrllibGithubLandingTransport(secret))

    def _default_pr_opener(self) -> ProtocolPinPrOpener:
        return ScmWritePrOpener(
            bus_write_call(
                lane_env=self._config.bus_lane_env,
                default_lane=self._config.default_bus_lane,
                timeout_s=self._config.bus_timeout_s,
            )
        )


__all__ = ["HandlerReceiptGatePinAdvanceEffect", "load_effect_config"]
