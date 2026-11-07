# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four outside reads of the dev-merge redeploy trigger, each behind a protocol.

The handlers decide; these read git, the deploy-path classifier and the dev lane. A real
adapter and an in-memory one satisfy each, so one suite runs on both (plan section 3.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class MergeFilesUnreadableError(RuntimeError):
    """The merge's changed files could not be read from the repository clone."""


class ClassifierUnavailableError(RuntimeError):
    """The deploy-path classifier could not be loaded or run."""


class BuildContextRefUnreadableError(RuntimeError):
    """The build-context repository's tip could not be resolved to a commit."""


class LaneUnreadableError(RuntimeError):
    """The dev lane's vendored revision, or its ancestry to a merge, could not be read."""


class ProtocolMergeFiles(Protocol):
    def changed_files(
        self, clone_dir: str, base: str, merge_sha: str
    ) -> tuple[str, ...]:
        """Paths a squash merge changed against its first parent.

        Raises ``MergeFilesUnreadableError`` when the clone cannot supply the merge.
        """
        ...


class ProtocolRuntimePathClassifier(Protocol):
    def runtime_paths(
        self, repo: str, clone_dir: str, merge_sha: str, files: Sequence[str]
    ) -> tuple[str, ...]:
        """The subset of ``files`` that changes what the lane runs.

        Raises ``ClassifierUnavailableError`` when the question cannot be answered; the caller
        never reads that as "not runtime".
        """
        ...


class ProtocolBuildContextRef(Protocol):
    def resolve(self, clone_dir: str, branch: str) -> str:
        """The 40-hex commit at the tip of ``branch`` in the build-context clone."""
        ...


class ProtocolLaneRevision(Protocol):
    def vendored_revision(self, source_repo: str, section: str) -> str:
        """The commit the lane's running build records for ``source_repo`` (40 hex).

        ``section`` is the key under ``per_repo_vcs_provenance`` that holds it.
        """
        ...

    def lane_carries(
        self, clone_dir: str, base: str, merge_sha: str, lane_sha: str
    ) -> bool:
        """True when ``lane_sha`` is ``merge_sha`` or a descendant of it."""
        ...
