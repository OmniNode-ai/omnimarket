# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSiblingPremiseRead: one release premise read, from clone and receipts to verdict.

Resolves the candidate (given, or the newest first-parent commit whose changed files
touch a runtime path), reads the dev-merge redeploy receipts the overlay's lookback
names, has ``HandlerSiblingPremise`` decide, then publishes one verdict event. Every
read ends either in its verdict or in one typed failure event on the failure topic:
a repo the overlay does not name, an unreadable clone, a classifier that cannot
answer, a walk with no runtime ancestor, or a verdict the sink refuses. Silence is
not an outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from omnimarket.events.sibling_premise_request import (
    ModelSiblingPremiseRequest,
)
from omnimarket.events.sibling_premise_verdict import (
    ModelSiblingPremiseVerdict,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    ClassifierUnavailableError,
    MergeFilesUnreadableError,
    ProtocolMergeFiles,
    ProtocolRuntimePathClassifier,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.protocol_sibling_premise_read import (
    ProtocolBranchHistory,
    ProtocolReceiptSource,
    ProtocolVerdictSink,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_command import (
    ModelSiblingPremiseReadCommand,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_failed import (
    EnumSiblingPremiseReadFailure,
    ModelSiblingPremiseReadFailed,
    SiblingPremiseReadError,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_topics import (
    ModelSiblingPremiseReadTopics,
)
from omnimarket.nodes.node_release_sibling_premise_effect.overlay import (
    ModelSiblingPremiseReadOverlay,
    ModelSiblingPremiseRepo,
)

_MESSAGE_LIMIT = 2000
logger = logging.getLogger(__name__)


class HandlerSiblingPremiseRead:
    """One release-train premise read per command, ending in one published event."""

    def __init__(
        self,
        overlay: ModelSiblingPremiseReadOverlay,
        *,
        receipts: ProtocolReceiptSource,
        history: ProtocolBranchHistory,
        files: ProtocolMergeFiles,
        classifier: ProtocolRuntimePathClassifier,
        decide: HandlerSiblingPremise,
        sink: ProtocolVerdictSink,
        clock: Callable[[], datetime],
        topics: ModelSiblingPremiseReadTopics,
    ) -> None:
        self._overlay = overlay
        self._receipts = receipts
        self._history = history
        self._files = files
        self._classifier = classifier
        self._decide = decide
        self._sink = sink
        self._clock = clock
        self._topics = topics

    async def handle(
        self, command: ModelSiblingPremiseReadCommand
    ) -> ModelSiblingPremiseVerdict:
        """Decide the premise for ``command.repo`` and publish its one event."""
        try:
            return await self._handle(command)
        except SiblingPremiseReadError as error:
            try:
                await self._sink.publish(
                    self._topics.failure,
                    ModelSiblingPremiseReadFailed(
                        code=error.code,
                        repo=command.repo,
                        message=str(error)[-_MESSAGE_LIMIT:],
                        observed_at=self._clock(),
                    ).model_dump(mode="json"),
                    uuid4(),
                )
            except Exception:
                # The read's own typed error is the answer; a sink that also refuses the
                # failure event must not replace it.
                logger.warning(
                    "the failure event for %s could not be published",
                    command.repo,
                    exc_info=True,
                )
            raise

    async def _handle(
        self, command: ModelSiblingPremiseReadCommand
    ) -> ModelSiblingPremiseVerdict:
        repo = self._overlay.repo(command.repo)
        if repo is None:
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.REPO_NOT_IN_OVERLAY,
                f"{command.repo} is not in the overlay",
            )
        candidate = command.candidate_sha or self._runtime_ancestor(command.repo, repo)
        merged_at = self._history.commit_time(repo.clone_dir, candidate)
        descendants = self._history.descendants(
            repo.clone_dir, repo.base_branch, candidate
        )
        receipts = await self._receipts.read(
            self._topics.receipt,
            since=self._clock() - timedelta(hours=self._overlay.lookback_hours),
        )
        verdict = self._decide.handle(
            ModelSiblingPremiseRequest(
                repo=command.repo,
                candidate_sha=candidate,
                candidate_merged_at=merged_at,
                descendant_shas=descendants,
                receipts=receipts,
                as_of=self._clock(),
                pending_window_seconds=self._overlay.pending_window_seconds,
            )
        )
        skipped = self._receipts.skipped()
        if skipped > 0:
            suffix = f" ({skipped} records on the topic were not receipts)"
            verdict = verdict.model_copy(
                update={"detail": (verdict.detail + suffix)[-_MESSAGE_LIMIT:]}
            )
        await self._publish(verdict)
        return verdict

    def _runtime_ancestor(self, repo_name: str, repo: ModelSiblingPremiseRepo) -> str:
        """The newest first-parent commit whose changed files touch a runtime path."""
        for sha in self._history.first_parent_walk(
            repo.clone_dir, repo.base_branch, self._overlay.max_walk_commits
        ):
            try:
                changed = self._files.changed_files(
                    repo.clone_dir, repo.base_branch, sha
                )
            except MergeFilesUnreadableError as exc:
                raise SiblingPremiseReadError(
                    EnumSiblingPremiseReadFailure.CLONE_UNREADABLE,
                    f"the changed files of {sha[:12]} could not be read: {exc}",
                ) from exc
            try:
                runtime = self._classifier.runtime_paths(
                    repo_name, repo.clone_dir, sha, changed
                )
            except ClassifierUnavailableError as exc:
                raise SiblingPremiseReadError(
                    EnumSiblingPremiseReadFailure.CLASSIFIER_UNAVAILABLE,
                    f"the deploy-path classifier could not answer for {sha[:12]}: {exc}",
                ) from exc
            if runtime:
                return sha
        raise SiblingPremiseReadError(
            EnumSiblingPremiseReadFailure.NO_RUNTIME_ANCESTOR,
            f"none of the last {self._overlay.max_walk_commits} first-parent commits of "
            f"{repo.base_branch} touches a runtime path",
        )

    async def _publish(self, verdict: ModelSiblingPremiseVerdict) -> None:
        try:
            await self._sink.publish(
                self._topics.verdict,
                verdict.model_dump(mode="json"),
                verdict.correlation_id or uuid4(),
            )
        except Exception as exc:
            raise SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.PUBLISH_FAILED,
                f"{type(exc).__name__}: {exc}"[-_MESSAGE_LIMIT:],
            ) from exc


__all__ = [
    "HandlerSiblingPremiseRead",
]
