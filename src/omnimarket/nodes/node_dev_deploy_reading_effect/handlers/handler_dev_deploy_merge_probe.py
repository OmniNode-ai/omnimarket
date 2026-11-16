# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one named dev merge against the deploy agent and the running build (OMN-23655): read-only, writes no row.

``handle(ModelDevDeployMergeProbeCommand) -> ModelDevDeployMergeProbeResult``. The merge's repository, its clone and its
place in the runtime's build record come from the overlay (``deploy_agent.merge_repos``); the command carries only the
repository name and the merge's commit. The judgement is HandlerDevDeployReading's, in its deploy-agent mode, so a
named merge reads exactly as the hourly reading would read it: deployed (WORKING), queued (UNKNOWN with its queue
position), failed (NOT WORKING with the job id).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReadingRequest,
    ModelLineageFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers.handler_dev_deploy_reading import (
    HandlerDevDeployReading,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    DeployAgentSource,
    Fetch,
    GitLineageSource,
    Runner,
    RuntimeProbeSource,
    http_get,
    run_process,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
)


class ModelDevDeployMergeProbeCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repo: str = Field(min_length=1)
    #: A full or abbreviated commit of the repository's dev ref; resolved in the clone.
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class ModelDevDeployMergeProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repo: str
    commit: str
    sha: str | None = None
    verdict: EnumReadingVerdict | None = None
    reasons: tuple[str, ...] = ()
    #: Why the merge could not be read at all (unknown repository, commit not in the clone).
    failure: str | None = None
    running_sha: str | None = None
    commands_ahead: int | None = None
    last_job_id: str | None = None
    last_job_status: str | None = None


class HandlerDevDeployMergeProbe:
    def __init__(
        self,
        overlay: ModelDevDeployReadingOverlay,
        environ: Mapping[str, str],
        clock: Callable[[], datetime],
        *,
        run: Runner = run_process,
        fetch: Fetch = http_get,
    ) -> None:
        self._overlay = overlay
        self._environ = environ
        self._clock = clock
        self._run = run
        self._fetch = fetch

    def _git(self, clone: str, *args: str) -> str:
        done = self._run(
            ["git", "-C", clone, *args],
            None,
            self._overlay.merges.git_timeout_seconds,
        )
        if done.returncode:
            raise ValueError(f"git {args[0]} exited {done.returncode}")
        return done.stdout.decode().strip()

    def handle(
        self, command: ModelDevDeployMergeProbeCommand
    ) -> ModelDevDeployMergeProbeResult:
        base = {"source_repo": command.source_repo, "commit": command.commit}
        overlay = self._overlay
        repo = next(
            (
                r
                for r in overlay.deploy_agent.merge_repos
                if r.source_repo == command.source_repo
            ),
            None,
        )
        if repo is None:
            return ModelDevDeployMergeProbeResult(
                **base, failure="the overlay names no such merge repository"
            )
        clone = self._environ.get(repo.clone_path_env, "").strip()
        if not clone:
            return ModelDevDeployMergeProbeResult(
                **base, failure=f"{repo.clone_path_env} is not set"
            )
        try:
            sha = self._git(
                clone, "rev-parse", "--verify", f"{command.commit}^{{commit}}"
            )
            stamp = self._git(clone, "show", "-s", "--format=%cI", sha)
            merged_at = datetime.fromisoformat(stamp).astimezone(UTC)
            on_ref = self._run(
                ["git", "-C", clone, "merge-base", "--is-ancestor", sha, repo.ref],
                None,
                overlay.merges.git_timeout_seconds,
            )
            if on_ref.returncode != 0:
                return ModelDevDeployMergeProbeResult(
                    **base, sha=sha, failure=f"the commit is not on {repo.ref}"
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return ModelDevDeployMergeProbeResult(
                **base, failure=f"clone unreadable: {type(exc).__name__}"
            )
        now = self._clock().astimezone(UTC)
        agent = DeployAgentSource(
            overlay.deploy_agent, overlay.probe, self._environ, self._fetch, self._run
        )
        deploy = agent.source(repo.provenance_section, repo.source_repo)
        lineage: tuple[ModelLineageFact, ...] = ()
        probes: tuple[ModelRuntimeProbeFact, ...] = ()
        probes_error: str | None
        if deploy.facts is None:
            probes_error = f"no running sha to expect: {deploy.unread_reason}"
        else:
            merge_settings = overlay.merges.model_copy(
                update={"clone_path_env": repo.clone_path_env, "ref": repo.ref}
            )
            lineage = (
                GitLineageSource(merge_settings, self._environ, self._run).pair(
                    sha, deploy.facts.running_sha
                ),
            )
            probes, probes_error = RuntimeProbeSource(
                overlay.probe, self._environ, self._fetch, self._run
            ).read(now, deploy.facts.running_sha)
        thresholds = overlay.thresholds
        try:
            reading = HandlerDevDeployReading().handle(
                ModelDevDeployReadingRequest(
                    now=now,
                    required_lane=thresholds.required_lane,
                    receipt_deadline_seconds=thresholds.receipt_deadline_seconds,
                    probe_max_age_seconds=thresholds.probe_max_age_seconds,
                    merges=(
                        ModelRuntimeMerge(
                            sha=sha, merged_at=merged_at, runtime_affecting=True
                        ),
                    ),
                    receipts=(),
                    probes=probes,
                    lineage_rule=overlay.lineage,
                    lineage=lineage,
                    receipts_unread_reason=None,
                    probes_unread_reason=probes_error,
                    deploy_source=deploy,
                )
            )
        except ValidationError as exc:
            return ModelDevDeployMergeProbeResult(
                **base,
                sha=sha,
                failure=f"facts-refused:{sorted({'.'.join(map(str, e['loc'])) for e in exc.errors()})}",
            )
        facts = deploy.facts
        return ModelDevDeployMergeProbeResult(
            **base,
            sha=sha,
            verdict=reading.verdict,
            reasons=reading.reasons,
            running_sha=None if facts is None else facts.running_sha,
            commands_ahead=None if facts is None else facts.commands_ahead,
            last_job_id=None if facts is None else facts.last_job_id,
            last_job_status=None if facts is None else facts.last_job_status,
        )


__all__ = [
    "HandlerDevDeployMergeProbe",
    "ModelDevDeployMergeProbeCommand",
    "ModelDevDeployMergeProbeResult",
]
