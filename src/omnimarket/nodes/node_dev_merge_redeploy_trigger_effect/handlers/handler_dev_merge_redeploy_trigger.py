# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevMergeRedeployTrigger -- decide whether a dev merge rebuilds the dev lane (OMN-22478).

Canonical def-B handler: ``handle(event: ModelRepoMerged) -> ModelDevMergeDecision``. It
replaces the Actions trigger job (``runtime-rebuild-trigger.yml``) for a merge it is
configured for: the same question (does the merge change what the lane runs), the same
classifier, the same start command, asked on a lab host from a bus event instead of on a
CI runner from a ``pull_request`` event.

Fail-closed in every direction. A merge whose files, classifier or build-context ref cannot be
read is a typed failure outcome and publishes no command; it is never read as "not runtime".
The correlation id is derived from the repository and merge sha, so a replayed merge event
names the same redeploy and the host can recognise it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumRedeployScope,
    EnumRuntimeLane,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    BuildContextRefUnreadableError,
    ClassifierUnavailableError,
    MergeFilesUnreadableError,
    ProtocolBuildContextRef,
    ProtocolMergeFiles,
    ProtocolRuntimePathClassifier,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_decision import (
    ModelDevMergeDecision,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployPolicy,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_start_command import (
    ModelDevMergeStartCommand,
)

CORRELATION_NAMESPACE = uuid.NAMESPACE_URL


def correlation_id_for(repo: str, merge_sha: str) -> uuid.UUID:
    """One redeploy per merge commit: a replayed merge event names the same id."""
    return uuid.uuid5(
        CORRELATION_NAMESPACE, f"onex:dev-merge-redeploy:{repo}@{merge_sha}"
    )


class HandlerDevMergeRedeployTrigger:
    """Classify one merge and build the start command for it."""

    def __init__(
        self,
        policy: ModelDevMergeRedeployPolicy,
        *,
        files: ProtocolMergeFiles,
        classifier: ProtocolRuntimePathClassifier,
        build_context: ProtocolBuildContextRef,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._files = files
        self._classifier = classifier
        self._build_context = build_context
        self._clock = clock

    def handle(self, event: ModelRepoMerged) -> ModelDevMergeDecision:
        cid = correlation_id_for(event.repo, event.merge_sha)
        repo = self._policy.for_repo(event.repo)
        if repo is None:
            return self._declined(
                cid,
                EnumDevMergeOutcome.DECLINED_REPO,
                f"{event.repo} is not configured",
            )
        if event.base not in repo.base_branches:
            return self._declined(
                cid,
                EnumDevMergeOutcome.DECLINED_BASE,
                f"base {event.base!r} is not one of {list(repo.base_branches)}",
            )
        try:
            files = self._files.changed_files(
                repo.clone_dir, event.base, event.merge_sha
            )
        except MergeFilesUnreadableError as exc:
            return self._failed(cid, EnumDevMergeOutcome.FAILED_FILES_UNREADABLE, exc)
        try:
            paths = self._classifier.runtime_paths(
                event.repo, repo.clone_dir, event.merge_sha, files
            )
        except ClassifierUnavailableError as exc:
            return self._failed(cid, EnumDevMergeOutcome.FAILED_CLASSIFIER, exc)
        if not paths:
            return self._declined(
                cid,
                EnumDevMergeOutcome.DECLINED_NOT_RUNTIME,
                f"{len(files)} changed file(s), none reaches the lane",
            )
        try:
            ref = self._build_context.resolve(
                repo.build_context_clone_dir, repo.build_context_branch
            )
        except BuildContextRefUnreadableError as exc:
            return self._failed(
                cid, EnumDevMergeOutcome.FAILED_BUILD_CONTEXT_REF, exc, len(paths)
            )
        command = ModelDevMergeStartCommand(
            correlation_id=cid,
            scope=EnumRedeployScope.FULL,
            git_ref=ref,
            runtime_lane=EnumRuntimeLane.DEV,
            build_source=EnumBuildSource.WORKSPACE,
            requested_by=f"{self._policy.requested_by}/{repo.source_repo}/pr-{event.pr_number}",
            requested_at=self._clock(),
        )
        return ModelDevMergeDecision(
            correlation_id=cid,
            outcome=None,
            command=command,
            runtime_paths=len(paths),
            message=f"{len(paths)} runtime path(s) of {len(files)} changed",
        )

    @staticmethod
    def _declined(
        cid: uuid.UUID, outcome: EnumDevMergeOutcome, message: str
    ) -> ModelDevMergeDecision:
        return ModelDevMergeDecision(
            correlation_id=cid, outcome=outcome, message=message
        )

    @staticmethod
    def _failed(
        cid: uuid.UUID,
        outcome: EnumDevMergeOutcome,
        exc: Exception,
        runtime_paths: int = 0,
    ) -> ModelDevMergeDecision:
        return ModelDevMergeDecision(
            correlation_id=cid,
            outcome=outcome,
            runtime_paths=runtime_paths,
            message=f"{type(exc).__name__}: {exc}"[-2000:],
        )


__all__ = [
    "CORRELATION_NAMESPACE",
    "HandlerDevMergeRedeployTrigger",
    "correlation_id_for",
]
