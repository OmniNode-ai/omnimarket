# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start one cause lane for a red landing-branch head, once per dispatch key (OMN-17427).

Ruling 2026-10-14T03:57:46Z (item 5): a component alarm starts a fix lane automatically, one per
cause. node_pr_lifecycle_orchestrator asks (``dev-cause-lane-requested``) and records who owns the
cause; this node is the receiver it names, on the host that dispatches lanes. The request's
``dispatch_key`` is the identity (formal/ci_red_triage, OneStartPerIdentity with ReceiverDedup):
the key is taken in a durable ledger before the runner is called, so a redelivered or replayed
request, or a restart, starts no second lane. A start that fails keeps its key taken and says so.

With ``act`` false the node answers ``dry_run`` and takes no key, so enabling it later still starts
the lane for a request redelivered after.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs

from omnimarket.models.dev_head_red import ModelDevCauseLaneRequest
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.models import (
    EnumDevCauseLaneStartOutcome,
    ModelDevCauseLaneStartResult,
)
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.protocols import (
    ProtocolCauseLaneStarter,
    ProtocolDispatchKeyLedger,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    lane_model_refusal,
)

_FLAG_WORDS = {"true": True, "false": False}
_BRIEF = """\
ROUTE: band=B2 score=6 dims=S1A1R1V1N1C1J0 dest=remote model={model} effort=high

# Lane {lane}: fix forward the red {cause} on {repo}@{branch}

The landing branch head {head_sha} of {repo} is red on the check "{check}" (observed {observed_at}).
Operator ruling 2026-10-14T03:57:46Z: fix forward on dev, no revert, and a component alarm starts
one fix lane per cause.

Task: find the cause of the red {check} on {branch}, fix it in a worktree built from the branch
head, and land the fix as a squash pull request against {branch}. Read the failing job's log from
the PR watcher state and the canonical clone, never from the hosting API.
"""


def receiver_act_flag(value: object) -> bool:
    """The contract's act flag: a bool, or an overlay reference expanding to true or false."""
    if isinstance(value, str):
        word = expand_contract_env_refs(value)
        if word not in _FLAG_WORDS:
            raise ValueError(
                f"dev_cause_lane_receiver.act must be true or false, not {word!r}"
            )
        return _FLAG_WORDS[word]
    if not isinstance(value, bool):
        raise ValueError("dev_cause_lane_receiver.act must be true or false")
    return value


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(
            f"{name} is unset: the receiver cannot start a lane without it"
        )
    return value


class HandlerDevCauseLaneStart:
    """Answer each dev cause-lane request with one outcome; start at most one lane per key."""

    def __init__(
        self,
        *,
        act: bool | None = None,
        ledger: ProtocolDispatchKeyLedger | None = None,
        runner: ProtocolCauseLaneStarter | None = None,
    ) -> None:
        """Every argument left ``None`` is read from the contract; acting needs the host's env."""
        if act is None:
            with (
                Path(__file__).resolve().parents[1] / "contract.yaml"
            ).open() as stream:
                act = receiver_act_flag(
                    yaml.safe_load(stream)["dev_cause_lane_receiver"]["act"]
                )
        self._act = act
        self._ledger = ledger
        self._runner = runner
        if act and (ledger is None or runner is None):
            from ..protocols.local_dev_cause_lane_adapters import (
                FileDispatchKeyLedger,
                SubprocessCauseLaneStarter,
            )

            state_dir = Path(_required_env("ONEX_DEV_CAUSE_LANE_STATE_DIR"))
            self._ledger = ledger or FileDispatchKeyLedger(state_dir)
            self._runner = runner or SubprocessCauseLaneStarter(
                Path(_required_env("ONEX_REMOTE_LANE_RUNNER")),
                _required_env("ONEX_DEV_CAUSE_LANE_TICKET"),
                state_dir / "briefs",
            )

    @staticmethod
    def _answer(
        request: ModelDevCauseLaneRequest,
        outcome: EnumDevCauseLaneStartOutcome,
        detail: str = "",
    ) -> ModelDevCauseLaneStartResult:
        return ModelDevCauseLaneStartResult(
            correlation_id=request.correlation_id,
            dispatch_key=request.dispatch_key,
            lane=request.lane,
            cause=request.cause,
            outcome=outcome,
            detail=detail,
        )

    def handle(self, request: ModelDevCauseLaneRequest) -> ModelDevCauseLaneStartResult:
        if not self._act:
            return self._answer(
                request, EnumDevCauseLaneStartOutcome.DRY_RUN, "act=false"
            )
        if self._ledger is None or self._runner is None:
            raise ValueError("an acting receiver needs its ledger and its starter")
        if refusal := lane_model_refusal(request.model, lane_kind="cause-red"):
            return self._answer(request, EnumDevCauseLaneStartOutcome.FAILED, refusal)
        if not self._ledger.claim(request.dispatch_key, request.lane):
            return self._answer(request, EnumDevCauseLaneStartOutcome.DUPLICATE)
        brief = _BRIEF.format(
            model=request.model,
            lane=request.lane,
            cause=request.cause,
            repo=request.repo,
            branch=request.branch,
            head_sha=request.head_sha,
            check=request.check,
            observed_at=request.observed_at,
        )
        receipt = self._runner.start(request, brief)
        outcome = (
            EnumDevCauseLaneStartOutcome.STARTED
            if receipt.started
            else EnumDevCauseLaneStartOutcome.FAILED
        )
        self._ledger.record(request.dispatch_key, outcome.value, receipt.detail)
        return self._answer(request, outcome, receipt.detail)
