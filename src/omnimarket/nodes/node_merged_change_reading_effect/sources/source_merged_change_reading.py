# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real sources of the merged-change reading: merge shas from the clones, the build provenance, the runtime's log.

The change to node map is an overlay document; the build facts reuse the merged-and-running count's provenance
reader (a merge in the history of the running build's recorded ref is in the build); the runtime facts are
``SourceRuntimeFacts``. A repository the provenance file does not record is named unread, never not in the build.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
)
from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrLogLabels,
    ModelMcrRunnerOverlay,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_runtime_log import (
    SourceRuntimeFacts,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_deployed_provenance import (
    SourceDeployedProvenance,
)


class SourceMergedChangeReading:
    """``ProtocolMergedChangeSources`` over the registry clones, the provenance file and the runtime host."""

    def __init__(
        self,
        *,
        runners: ModelMcrRunnerOverlay,
        provenance: SourceDeployedProvenance | None,
        provenance_repos: Sequence[str],
        runtime: SourceRuntimeFacts | None,
        labels: ModelMcrLogLabels,
        home: Path,
    ) -> None:
        self._runners = runners
        self._provenance = provenance
        self._provenance_repos = tuple(provenance_repos)
        self._runtime = runtime
        self._labels = labels
        self._home = home
        self._cache: tuple[ModelMcrRuntimeFacts, datetime | None] | None = None

    def _merge_sha(self, change: ModelMcrChange) -> str:
        if change.merge_sha:
            return change.merge_sha
        pattern = self._labels.merge_subject_pattern.format(number=change.number)
        for branch in self._labels.merge_branches:
            try:
                done = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self._home / change.repo),
                        "log",
                        f"origin/{branch}",
                        "--format=%H",
                        "-E",
                        f"--grep={pattern}",
                        "-n",
                        "1",
                    ],
                    capture_output=True,
                    timeout=self._labels.git_timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            sha = done.stdout.decode().strip() if done.returncode == 0 else ""
            if sha and re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
        return ""

    def resolve_changes(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[ModelMcrChange, ...]:
        return tuple(
            c.model_copy(update={"merge_sha": self._merge_sha(c)}) for c in changes
        )

    def read_runners(self) -> tuple[ModelMcrRunner, ...]:
        return self._runners.runners

    def _runtime_read(self) -> tuple[ModelMcrRuntimeFacts, datetime | None]:
        if self._cache is None:
            if self._runtime is None:
                self._cache = (
                    ModelMcrRuntimeFacts(
                        unread=(
                            "loaded_nodes",
                            "subscriptions",
                            "published",
                            "unit_runs",
                        )
                    ),
                    None,
                )
            else:
                self._cache = self._runtime.read(self._runners.runners)
        return self._cache

    def read_build(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[tuple[ModelMcrBuildFact, ...], tuple[str, ...], datetime | None]:
        started = self._runtime_read()[1]
        store = self._store_repos()
        recorded = set(self._provenance_repos) - store
        repos = sorted({c.repo for c in changes})
        unread = {r for r in repos if r not in recorded and r not in store}
        unread.update(f"{c.repo}#{c.number}" for c in changes if not c.merge_sha)
        store_facts, store_unread = self._store_facts(changes)
        unread.update(store_unread)
        facts: tuple[ModelMcrBuildFact, ...] = ()
        if self._provenance is None:
            unread.update(recorded & set(repos))
        else:
            merges = [
                (c.repo, c.number, c.merge_sha)
                for c in changes
                if c.repo in recorded and c.merge_sha
            ]
            read = self._provenance.read(merges)
            cause = read.unread_cause
            if cause:
                if ":" not in cause or cause.startswith("build provenance unreadable"):
                    unread.update(recorded & set(repos))
                else:
                    unread.update(r for r in recorded if f"{r}:" in cause)
            running = {(e.repo, e.number) for e in read.evidence}
            facts = tuple(
                ModelMcrBuildFact(
                    repo=c.repo,
                    number=c.number,
                    in_build=(c.repo, c.number) in running,
                    source="build provenance",
                )
                for c in changes
                if c.repo in recorded and c.repo not in unread and c.merge_sha
            )
        return (*facts, *store_facts), tuple(sorted(unread)), started

    def _store_repos(self) -> set[str]:
        return {c.repo for c in self._runners.store_checkouts}

    def _store_facts(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[tuple[ModelMcrBuildFact, ...], set[str]]:
        """Build facts for repositories the runtime mounts from a checkout (the overlay store)."""
        facts: list[ModelMcrBuildFact] = []
        unread: set[str] = set()
        for checkout in self._runners.store_checkouts:
            mine = [c for c in changes if c.repo == checkout.repo]
            if not mine:
                continue
            unread.update(f"{c.repo}#{c.number}" for c in mine if not c.merge_sha)
            known = [c for c in mine if c.merge_sha]
            try:
                if self._runtime is None:
                    raise ValueError("no runtime host reader")
                answers = self._runtime.checkout_ancestry(
                    checkout.path, [c.merge_sha for c in known]
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                unread.add(checkout.repo)
                continue
            for change in known:
                answer = answers.get(change.merge_sha)
                if answer is None:
                    unread.add(f"{change.repo}#{change.number}")
                    continue
                facts.append(
                    ModelMcrBuildFact(
                        repo=change.repo,
                        number=change.number,
                        in_build=answer,
                        source=self._labels.store_source,
                    )
                )
        return tuple(facts), unread

    def read_runtime(self, runners: Sequence[ModelMcrRunner]) -> ModelMcrRuntimeFacts:
        return self._runtime_read()[0]


def now_utc() -> datetime:
    return datetime.now(UTC)
