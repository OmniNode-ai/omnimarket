# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold handler of node_dev_roll_window_gate_effect (OMN-19998).

One dev deploy command in (forwarded by node_redeploy_deploy_effect while the gate is
live), one held record or one typed refusal out. A held command is stored durably and
published to the deploy agent only by the tick handler, when the batching decision says
roll now. A command the gate cannot hold (not a CI branch-tip request, a pinned image,
a gate that is off, a full store) is never dropped: the refusal event is followed by the
command itself, marked released, so the deploy effect publishes it as it did before the
gate. Only a repeat of a command already released is answered with the refusal alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRefusal,
    ModelDevRollGateHeld,
    ModelDevRollGateRefused,
    ModelDevRollWindowGatePolicy,
    ModelHeldDevRoll,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")

Result = ModelDevRollGateHeld | Sequence[BaseModel]


class HandlerDevRollWindowHold:
    """``handle(ModelDeployPublishCommand) -> ModelDevRollGateHeld | [ModelDevRollGateRefused, the released command]``."""

    def __init__(
        self,
        store: ProtocolDevRollGateStore | None = None,
        *,
        policy: ModelDevRollWindowGatePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._context = DevRollGateContext(
            store,
            policy=policy,
            environ=environ,
            clock=clock,
        )

    def _refused(
        self,
        request: ModelDeployPublishCommand,
        reason: EnumDevRollGateRefusal,
        detail: str,
        now: datetime,
    ) -> Sequence[BaseModel]:
        refusal = ModelDevRollGateRefused(
            command_correlation_id=request.correlation_id,
            reason=reason,
            detail=detail,
            observed_at=now,
        )
        if reason is EnumDevRollGateRefusal.ALREADY_RELEASED:
            return [refusal]
        return [refusal, request.model_copy(update={"roll_window_released": True})]

    def handle(self, request: ModelDeployPublishCommand) -> Result:
        policy = self._context.policy()
        now = self._context.now()
        if policy.mode == "off":
            return self._refused(
                request, EnumDevRollGateRefusal.GATE_OFF, "the gate overlay is off", now
            )
        if request.runtime_lane is not EnumRuntimeLane.DEV:
            return self._refused(
                request,
                EnumDevRollGateRefusal.NOT_A_DEV_COMMAND,
                f"runtime_lane is {request.runtime_lane.value}",
                now,
            )
        issuer = re.fullmatch(policy.issuer_repository_pattern, request.requested_by)
        if issuer is None:
            return self._refused(
                request,
                EnumDevRollGateRefusal.REQUESTER_NOT_A_CI_ISSUER,
                "requested_by does not match the issuer pattern",
                now,
            )
        repository = issuer.group("repository")
        if repository not in policy.known_repositories:
            return self._refused(
                request,
                EnumDevRollGateRefusal.REPOSITORY_UNKNOWN,
                f"repository {repository} is not a known repository",
                now,
            )
        if request.image_ref is not None or request.image_digest is not None:
            return self._refused(
                request,
                EnumDevRollGateRefusal.PINNED_IMAGE,
                "a pinned image is a promotion, never a branch-tip roll",
                now,
            )
        if request.git_ref is None or _SHA.match(request.git_ref) is None:
            return self._refused(
                request,
                EnumDevRollGateRefusal.GIT_REF_NOT_A_SHA,
                "git_ref is not a full lowercase commit sha",
                now,
            )
        store = self._context.store()
        state = store.load()
        command_id = str(request.correlation_id)
        if command_id in state.released_ids:
            return self._refused(
                request,
                EnumDevRollGateRefusal.ALREADY_RELEASED,
                "this command was already released to the deploy agent",
                now,
            )
        if any(
            item.command.correlation_id == request.correlation_id for item in state.held
        ):
            return ModelDevRollGateHeld(
                command_correlation_id=request.correlation_id,
                repository=repository,
                sha=request.git_ref,
                held_count=len(state.held),
                observed_at=now,
            )
        if len(state.held) >= policy.held_kept:
            return self._refused(
                request,
                EnumDevRollGateRefusal.HELD_FULL,
                f"{len(state.held)} commands are already held",
                now,
            )
        held = (
            *state.held,
            ModelHeldDevRoll(
                command=request, repository=repository, sha=request.git_ref, held_at=now
            ),
        )
        store.save(state.model_copy(update={"held": held}))
        return ModelDevRollGateHeld(
            command_correlation_id=request.correlation_id,
            repository=repository,
            sha=request.git_ref,
            held_count=len(held),
            observed_at=now,
        )
