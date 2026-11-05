# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request lab reviews from PR observations, before an Actions job fails.

The producer publishes the existing review command. It runs outside Actions,
uses the gateway for the current PR and a durable per-head acknowledgement,
and never invokes voters itself. Ambiguous publishes retry the same command id
that the dispatch receipt store already replays.
"""

# unimported-handler-allow: the runtime loads this handler from the node contract's handler_routing (operation request_pr_review), not from a Python import

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelPrTextResult,
)
from omnimarket.models.scm import EnumScmNode

from ..adapters.adapter_agent_review_ports import ScmBus
from ..models.model_agent_review import ModelAgentReviewRequest
from ..models.model_pr_review_trigger import (
    ModelPrReviewObservation,
    ModelPrReviewTriggerConfig,
    ModelPrReviewTriggerResult,
)
from ..protocols.protocol_pr_review_trigger import ProtocolPrReviewTriggerBus

OVERLAY_ID = "agent_review_pr_trigger"


class HandlerPrReviewTrigger:
    """PR-state event in, an acknowledged exact-head review command out."""

    def __init__(
        self,
        *,
        config: ModelPrReviewTriggerConfig | None = None,
        bus: ProtocolPrReviewTriggerBus | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._environ = environ

    async def handle(
        self, request: ModelPrReviewObservation
    ) -> ModelPrReviewTriggerResult:
        env = os.environ if self._environ is None else self._environ
        config = self._config or load_structured_overlay(
            OVERLAY_ID, ModelPrReviewTriggerConfig, env
        )

        def unchanged(reason: str, command_id: str = "") -> ModelPrReviewTriggerResult:
            return ModelPrReviewTriggerResult(
                outcome="unchanged", reason=reason, command_id=command_id
            )

        repositories = {repo.casefold() for repo in config.repositories}
        if request.repo.casefold() not in repositories:
            return unchanged("repository_out_of_scope")
        state = env.get("ONEX_STATE_DIR", "")
        root = env.get("OMNI_HOME", "")
        if not state or not root:
            raise ValueError("ONEX_STATE_DIR and OMNI_HOME are required")
        bus = self._bus or ScmBus(
            kind=config.scm_bus.kind,
            lane=config.scm_bus.lane,
            registry_root=Path(root),
            timeout_seconds=config.read_timeout_seconds,
        )
        current = ModelPrTextResult.model_validate(
            await bus.call(
                EnumScmNode.GATEWAY,
                ModelGithubGatewayRequest(
                    correlation_id=uuid4(),
                    operation=EnumGithubGatewayOperation.PR_TEXT,
                    repo=request.repo,
                    pr_number=request.pr_number,
                    limit=1,
                ),
            )
        )
        if (
            current.repo.casefold() != request.repo.casefold()
            or current.pr_number != request.pr_number
        ):
            raise ValueError("the gateway returned another pull request")
        if current.state.upper() != "OPEN" or current.merged:
            return unchanged("not_open")
        # The workflow also runs on drafts. Do not silently remove that coverage.
        if current.base_ref not in config.base_branches:
            return unchanged("base_out_of_scope")
        if request.head_sha and request.head_sha != current.head_sha:
            return unchanged("stale_observation")
        command = ModelAgentReviewRequest(
            # Matches the failed-voter producer's identity for a structural union.
            command_id=hashlib.sha256(
                f"{current.repo.lower()}#{current.pr_number}#{current.head_sha}".encode()
            ).hexdigest(),
            repo=current.repo,
            pr=current.pr_number,
            head_sha=current.head_sha,
        )
        journal = Path(state) / config.state_subdirectory
        journal.mkdir(parents=True, exist_ok=True)
        sent = journal / f"{command.command_id}.sent"
        with (journal / f"{command.command_id}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return unchanged("concurrent_request", command.command_id)
            try:
                if sent.exists() and sent.read_text() == command.command_id + "\n":
                    return unchanged("already_requested", command.command_id)
                await bus.publish_review(command)
                # Persist only an acknowledged publish. A crash before this fsync
                # can replay the same command, never a different review identity.
                pending = sent.with_suffix(f".{uuid4().hex}.tmp")
                with pending.open("w") as acknowledgement:
                    acknowledgement.write(command.command_id + "\n")
                    acknowledgement.flush()
                    os.fsync(acknowledgement.fileno())
                pending.replace(sent)
                directory_fd = os.open(journal, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return ModelPrReviewTriggerResult(
            outcome="requested", reason="current_pr_head", command_id=command.command_id
        )
