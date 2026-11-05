# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request a review when a required exact-head review produced no voter verdict.

The existing ci-run-failed stream is the seam: it carries repository, PR, head
and workflow identities, unlike a gate-accept terminal. Read the completed job
through the source-control node; neither a red check alone nor an unavailable
rung alone justifies a request. No gate, voter policy or login is changed.
"""

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.ci_red_triage import ModelCiRunFailedEvent
from omnimarket.models.model_agent_review_command import ModelAgentReviewRequest

from ..models.model_review_trigger import (
    ModelReviewTriggerConfig,
    ModelReviewTriggerResult,
)
from ..protocols.protocol_review_trigger import (
    ProtocolDispatchJournal,
    ProtocolReviewCommandPublisher,
    ProtocolReviewTriggerSource,
)
from .handler_dispatch_journal import FileDispatchJournal
from .handler_review_trigger_bus import ReviewTriggerBus, ScmReviewTriggerSource

NODE = "node_agent_review_trigger_effect"


def command_for(
    event: ModelCiRunFailedEvent, config: ModelReviewTriggerConfig
) -> ModelAgentReviewRequest:
    """A logical command ignores the workflow run, attempt and observation id."""
    repo = config.qualify_repo(event.repo)
    key = f"{repo.lower()}#{event.pr_number}#{event.head_sha}"
    return ModelAgentReviewRequest(
        command_id=hashlib.sha256(key.encode()).hexdigest(),
        repo=repo,
        pr=event.pr_number,
        head_sha=event.head_sha,
    )


def no_voter_verdict(text: str) -> bool:
    """Match emitted result lines, never mere source text or a generic CI red."""
    return (
        re.search(
            r"Quorum verdict: no_models \(threshold=\d+, models succeeded=0, distinct reviewers=0\)",
            text,
        )
        is not None
        and re.search(
            r"Hostile review gate accept: decision=unchanged reason=no_receipt_review(?:;|[ \t]*$)",
            text,
            re.MULTILINE,
        )
        is not None
    )


class HandlerAgentReviewTrigger:
    def __init__(
        self,
        *,
        config: ModelReviewTriggerConfig | None = None,
        source: ProtocolReviewTriggerSource | None = None,
        publisher: ProtocolReviewCommandPublisher | None = None,
        journal: ProtocolDispatchJournal | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._source = source
        self._publisher = publisher
        self._journal = journal
        self._environ = environ

    async def handle(self, request: ModelCiRunFailedEvent) -> ModelReviewTriggerResult:
        config = self._config or load_structured_overlay(
            NODE, ModelReviewTriggerConfig, self._environ
        )
        command = command_for(request, config)

        def unchanged(reason: str) -> ModelReviewTriggerResult:
            return ModelReviewTriggerResult(
                outcome="unchanged", reason=reason, command_id=command.command_id
            )

        if not set(request.failing_checks).intersection(config.required_review_checks):
            return unchanged("not_review_failure")
        env = os.environ if self._environ is None else self._environ
        journal = self._journal
        if journal is None:
            state = env.get("ONEX_STATE_DIR", "")
            if not state:
                raise ValueError("ONEX_STATE_DIR is required for the dispatch journal")
            journal = FileDispatchJournal(
                Path(state) / config.state_subdirectory,
                poll_seconds=config.lock_poll_seconds,
            )
        bus = ReviewTriggerBus(config, env)
        source = self._source or ScmReviewTriggerSource(bus, config)
        publisher = self._publisher or bus
        async with journal.lock(command.command_id):
            if journal.sent(command.command_id):
                return unchanged("already_requested")
            evidence = await source.evidence(request)
            if not any(
                item.required
                and item.head_sha == request.head_sha
                and no_voter_verdict(item.log_text)
                for item in evidence
            ):
                return unchanged("no_eligible_evidence")
            if await source.read_head(command.repo, command.pr) != command.head_sha:
                return unchanged("stale_head")
            await publisher.publish(command)
            journal.record(command.command_id)
        return ModelReviewTriggerResult(
            outcome="emitted", reason="no_voter_verdict", command_id=command.command_id
        )
