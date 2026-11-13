# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab source: the dev runtime's build provenance read over ssh, and each repository's dev merges read with git.

The provenance file names each repository's ``vcs_ref`` under ``per_repo_vcs_provenance`` (sections
``siblings`` and ``build_context``) and the build's ``build_time``. It does not record when the runtime
started, so ``running_since`` is the build time: a start event from the deploy agent replaces it
(five whys plan R1). A repository the file does not name is left out of the manifest and reads unknown.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError

from omnimarket.models.running_revision import (
    ModelBuildManifest,
    ModelBuildRevision,
    ModelDevMerge,
    ModelRepositoryMergeHistory,
    ModelRunningBuild,
    ModelRunningRevisionFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    Runner,
    run_process,
)
from omnimarket.nodes.node_dev_running_revision_effect.overlay import (
    ModelRunningRevisionSourceSettings,
    ModelSourceRepository,
)
from omnimarket.nodes.node_dev_running_revision_effect.protocols.protocol_running_revision_source import (
    RunningRevisionSourceError,
)

_SHA_LENGTH = 40


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class LabRunningRevisionSource:
    """Reads the provenance file through ssh and the merge histories through git, all through ``run``."""

    def __init__(
        self,
        settings: ModelRunningRevisionSourceSettings,
        environ: Mapping[str, str] | None = None,
        run: Runner = run_process,
    ) -> None:
        self._settings = settings
        self._environ = os.environ if environ is None else environ
        self._run = run

    def _provenance(self) -> dict[str, object]:
        s = self._settings.ssh
        host = self._environ.get(s.host_env, "").strip()
        if not host:
            raise RunningRevisionSourceError(f"{s.host_env} is not set")
        script = "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={s.compose_project} --filter "
                f"label=com.docker.compose.service={s.service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" cat {s.provenance_path}',
            ]
        )
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            host,
            "bash -s",
        ]
        try:
            done = self._run(argv, script.encode(), s.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunningRevisionSourceError(
                f"provenance read failed: {type(exc).__name__}"
            ) from exc
        if done.returncode:
            raise RunningRevisionSourceError(
                f"provenance read exited {done.returncode}"
            )
        try:
            document = json.loads(done.stdout)
        except ValueError as exc:
            raise RunningRevisionSourceError("provenance file is not JSON") from exc
        if not isinstance(document, dict):
            raise RunningRevisionSourceError("provenance file is not an object")
        return document

    def _git(
        self, clone: str, repository: str, *args: str
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._run(
                ["git", "-C", clone, *args], None, self._settings.git.timeout_seconds
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunningRevisionSourceError(
                f"{repository}: git failed: {type(exc).__name__}"
            ) from exc

    def _history(
        self,
        repo: ModelSourceRepository,
        running_sha: str | None,
        observed_at: datetime,
    ) -> ModelRepositoryMergeHistory:
        clone = self._environ.get(repo.clone_path_env, "").strip()
        if not clone:
            raise RunningRevisionSourceError(
                f"{repo.repository}: {repo.clone_path_env} is not set"
            )
        base: str | None = None
        if running_sha is not None:
            ancestor = self._git(
                clone,
                repo.repository,
                "merge-base",
                "--is-ancestor",
                running_sha,
                repo.ref,
            )
            if ancestor.returncode == 0:
                base = running_sha
        if base is None:
            before = (
                observed_at - timedelta(seconds=self._settings.git.lookback_seconds)
            ).isoformat()
            found = self._git(
                clone,
                repo.repository,
                "rev-list",
                "--first-parent",
                "-n",
                "1",
                f"--before={before}",
                repo.ref,
            )
            base = found.stdout.decode().strip() if found.returncode == 0 else ""
            if len(base) != _SHA_LENGTH:
                raise RunningRevisionSourceError(
                    f"{repo.repository}: no commit before the lookback on {repo.ref}"
                )
        log = self._git(
            clone,
            repo.repository,
            "log",
            "--first-parent",
            "--reverse",
            "--format=%H%x09%cI",
            f"{base}..{repo.ref}",
        )
        if log.returncode:
            raise RunningRevisionSourceError(
                f"{repo.repository}: git log exited {log.returncode}"
            )
        merges: list[ModelDevMerge] = []
        for line in log.stdout.decode().splitlines():
            sha, _, stamp = line.partition("\t")
            try:
                merges.append(ModelDevMerge(sha=sha, merged_at=_utc(stamp)))
            except (ValueError, ValidationError) as exc:
                raise RunningRevisionSourceError(
                    f"{repo.repository}: unreadable log line"
                ) from exc
        if len(merges) > self._settings.git.max_merges:
            raise RunningRevisionSourceError(
                f"{repo.repository}: {len(merges)} merges exceed max_merges"
            )
        try:
            return ModelRepositoryMergeHistory(
                repository=repo.repository,
                base_sha=base,
                tip_sha=merges[-1].sha if merges else base,
                merges=tuple(merges),
            )
        except ValidationError as exc:
            raise RunningRevisionSourceError(
                f"{repo.repository}: merge history is not ordered"
            ) from exc

    def read(
        self, correlation_id: UUID, observed_at: datetime
    ) -> ModelRunningRevisionFacts:
        document = self._provenance()
        try:
            build_time = _utc(str(document["build_time"]))
            sections = document["per_repo_vcs_provenance"]
            assert isinstance(sections, dict)
        except (KeyError, ValueError, AssertionError) as exc:
            raise RunningRevisionSourceError(
                "provenance file lacks build_time or per_repo_vcs_provenance"
            ) from exc
        revisions: list[ModelBuildRevision] = []
        running: dict[str, str | None] = {}
        for repo in self._settings.repositories:
            section = sections.get(repo.provenance_section, {})
            entry = section.get(repo.repository) if isinstance(section, dict) else None
            ref = entry.get("vcs_ref") if isinstance(entry, dict) else None
            running[repo.repository] = ref if isinstance(ref, str) else None
            if isinstance(ref, str):
                try:
                    revisions.append(
                        ModelBuildRevision(repository=repo.repository, sha=ref)
                    )
                except ValidationError as exc:
                    raise RunningRevisionSourceError(
                        f"{repo.repository}: vcs_ref is not a sha"
                    ) from exc
        manifest = ModelBuildManifest(
            build_id=str(document["build_time"]),
            built_at=build_time,
            source_hash=None,
            revisions=tuple(revisions),
            branch_installs=(),
        )
        histories = tuple(
            self._history(repo, running[repo.repository], observed_at)
            for repo in self._settings.repositories
        )
        return ModelRunningRevisionFacts(
            correlation_id=correlation_id,
            observed_at=observed_at,
            builds=(ModelRunningBuild(manifest=manifest, running_since=build_time),),
            histories=histories,
        )
