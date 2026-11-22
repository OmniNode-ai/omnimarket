# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The collector's real gateway port: nightly runs, junit artifacts and pull request records read through the
repository gateway (the operations behind ``onex scm run-list``, ``run-download``, ``run-view`` and ``pr-view``).

The gateway client is injected (``ProtocolGatewayRead``), so the same adapter runs in process on a host that holds
the token and over the bus on a lab host; this module opens no connection of its own. Every refusal, expired
artifact, unreadable archive or record that does not bind to its run is no evidence: it raises
:class:`GatewayReadError` or leaves the candidate's record or run empty, which the pure rules class as no
evidence, never as a pass.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from typing import TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from omnimarket.models.model_gateway_node_skew import (
    GatewayReadError as GatewayClientError,
)
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelGithubGatewayRequest,
    ModelPrHead,
    ModelPrHeadsResult,
    ModelRunJob,
    ModelRunJobsResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.models.test_selector_reading import (
    EnumFileOutcome,
    EnumShardConclusion,
    ModelAttributionCandidate,
    ModelCandidateRecord,
    ModelCandidateRun,
    ModelCandidateSelection,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.junit_outcomes import (
    JunitUnreadableError,
    read_junit,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelMissCollectPolicy,
    ModelMissCollectSettings,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
    ProtocolCommitRange,
    ProtocolGatewayRead,
)

_RESULT = TypeVar("_RESULT", bound=BaseModel)
_RECORD_MEMBER = "selection-record.json"
_PRECEDENCE: tuple[EnumFileOutcome, ...] = (
    EnumFileOutcome.ERROR,
    EnumFileOutcome.FAILED,
    EnumFileOutcome.PASSED,
    EnumFileOutcome.SKIPPED,
)


class _Selection(BaseModel):
    """The part of a selection the attribution reads. The record is written by another node: unknown keys are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    selected_paths: tuple[str, ...]
    split_count: int


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    pr_number: int | None
    head_sha: str
    checkout_sha: str
    run_id: int
    run_attempt: int
    executed_selection: _Selection
    selection_by_stage: dict[int, _Selection]


def _instant(text: str) -> AwareDatetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _conclusion(job: ModelRunJob) -> EnumShardConclusion:
    try:
        return EnumShardConclusion(job.conclusion or "")
    except ValueError:
        return EnumShardConclusion.FAILURE


class GatewayNightlyGateway:
    """``ProtocolNightlyGateway`` over the repository gateway and a first-parent commit range."""

    def __init__(
        self,
        *,
        reader: ProtocolGatewayRead,
        commits: ProtocolCommitRange,
        settings: ModelMissCollectSettings,
        policy: ModelMissCollectPolicy,
    ) -> None:
        self._reader = reader
        self._commits = commits
        self._settings = settings
        self._policy = policy
        self._shard_job = re.compile(policy.shard_job_pattern)

    # --- the gateway, typed -----------------------------------------------------------------

    def _read(self, request: ModelGithubGatewayRequest, kind: type[_RESULT]) -> _RESULT:
        try:
            result: GithubGatewayResult = self._reader.read(request)
        except GatewayClientError as exc:
            raise GatewayReadError(str(exc)) from exc
        if not isinstance(result, kind):
            raise GatewayReadError(
                f"gateway answered {type(result).__name__} to {request.operation.value}"
            )
        return result

    def _runs(
        self, repo: str, workflow: str, event: str, head_sha: str | None = None
    ) -> ModelWorkflowRunsResult:
        return self._read(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
                repo=repo,
                workflow=workflow,
                event=event,
                head_sha=head_sha,
                limit=MAX_LIST_LIMIT,
            ),
            ModelWorkflowRunsResult,
        )

    def _artifacts(
        self, repo: str, run_id: int, artifact_id: int | None = None
    ) -> ModelArtifactsResult:
        return self._read(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.ARTIFACTS,
                repo=repo,
                run_id=run_id,
                artifact_id=artifact_id,
                max_bytes=self._policy.artifact_max_bytes,
                limit=MAX_LIST_LIMIT,
            ),
            ModelArtifactsResult,
        )

    def _archive_member(
        self, repo: str, run_id: int, artifact_id: int, suffix: str
    ) -> list[bytes]:
        """The members of one artifact's zip whose name ends with ``suffix``; the archive's hash is checked."""
        result = self._artifacts(repo, run_id, artifact_id)
        if result.fetched is None:
            reason = (
                result.fetch_refused.value if result.fetch_refused else "no archive"
            )
            raise GatewayReadError(f"artifact {artifact_id} was not fetched: {reason}")
        try:
            raw = base64.b64decode(result.fetched.content_base64, validate=True)
        except binascii.Error as exc:
            raise GatewayReadError(f"artifact {artifact_id} is not base64") from exc
        if hashlib.sha256(raw).hexdigest() != result.fetched.sha256:
            raise GatewayReadError(f"artifact {artifact_id} fails its sha256")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise GatewayReadError(f"artifact {artifact_id} is not a zip") from exc
        members = [i for i in archive.infolist() if i.filename.endswith(suffix)]
        if sum(i.file_size for i in members) > self._policy.artifact_unzipped_max_bytes:
            raise GatewayReadError(f"artifact {artifact_id} unzips past the cap")
        return [archive.read(i) for i in members]

    # --- the nightly -------------------------------------------------------------------------

    def nightly_run(self, repo: str, slot_date: date) -> ModelNightlyRun | None:
        runs = self._runs(
            repo, self._settings.workflow_file, self._policy.nightly_event
        )
        dated = [
            run for run in runs.runs if _instant(run.created_at).date() == slot_date
        ]
        if not dated:
            if runs.truncated and all(
                _instant(run.created_at).date() > slot_date for run in runs.runs
            ):
                raise GatewayReadError(
                    f"the run list is cut before {slot_date.isoformat()}"
                )
            return None
        newest = max(dated, key=lambda run: _instant(run.created_at))
        return ModelNightlyRun(
            run_id=newest.id,
            completed=newest.status == "completed",
            head_sha=newest.head_sha,
        )

    def read_night(self, repo: str, run: ModelNightlyRun) -> ModelNightReading:
        prefix = re.compile(
            rf"^{re.escape(self._policy.nightly_artifact_prefix)}(\d+)$"
        )
        listing = self._artifacts(repo, run.run_id)
        shards: dict[int, ModelArtifactSummary] = {}
        for artifact in listing.artifacts:
            match = prefix.match(artifact.name)
            if match and not artifact.expired:
                shards[int(match.group(1))] = artifact
        if listing.truncated:
            raise GatewayReadError("the artifact list of the night is cut")
        outcomes: dict[str, EnumFileOutcome] = {}
        in_error = 0
        for artifact in shards.values():
            try:
                documents = self._archive_member(repo, run.run_id, artifact.id, ".xml")
                if not documents:
                    raise JunitUnreadableError("no junit report in the artifact")
                shard_outcomes: dict[str, EnumFileOutcome] = {}
                shard_collection_error = False
                for document in documents:
                    parsed, collection_error = read_junit(document)
                    shard_collection_error |= collection_error
                    for test_file, outcome in parsed.items():
                        known = shard_outcomes.get(test_file)
                        if known is None or _PRECEDENCE.index(
                            outcome
                        ) < _PRECEDENCE.index(known):
                            shard_outcomes[test_file] = outcome
            except (GatewayReadError, JunitUnreadableError):
                in_error += 1
                continue
            if shard_collection_error:
                in_error += 1
            for test_file, outcome in shard_outcomes.items():
                known = outcomes.get(test_file)
                if known is None or _PRECEDENCE.index(outcome) < _PRECEDENCE.index(
                    known
                ):
                    outcomes[test_file] = outcome
        return ModelNightReading(
            artifacts_found=len(shards), shards_in_error=in_error, outcomes=outcomes
        )

    def previous_night(self, repo: str, slot_date: date) -> ModelPreviousNight | None:
        """The night before, or None when it has no terminal run or is itself incomplete (never a baseline)."""
        run = self.nightly_run(repo, date.fromordinal(slot_date.toordinal() - 1))
        if run is None or not run.completed:
            return None
        reading = self.read_night(repo, run)
        if (
            reading.artifacts_found < self._policy.nightly_shard_count
            or reading.shards_in_error > 0
            or len(reading.outcomes) < self._policy.nightly_min_test_files
        ):
            return None
        return ModelPreviousNight(head_sha=run.head_sha, outcomes=reading.outcomes)

    # --- the merged pull requests --------------------------------------------------------------

    def candidates(
        self, repo: str, previous_head: str, head: str, retention_days: int
    ) -> tuple[ModelAttributionCandidate, ...]:
        commits = self._commits.first_parent(previous_head, head)
        if not commits:
            return ()
        heads = self._read(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.PR_HEADS,
                repo=repo,
                limit=MAX_LIST_LIMIT,
            ),
            ModelPrHeadsResult,
        )
        by_merge: dict[str, ModelPrHead] = {
            pr.merge_commit_sha: pr
            for pr in heads.prs
            if pr.state == "MERGED" and pr.merge_commit_sha is not None
        }
        newest = max(commit.committed_at for commit in commits)
        found: list[ModelAttributionCandidate] = []
        for commit in commits:
            pr = by_merge.get(commit.sha)
            if pr is None:
                if heads.truncated:
                    # Not on a cut list is not a direct push: fail closed.
                    raise GatewayReadError(
                        f"commit {commit.sha} is not on the cut pull request list"
                    )
                found.append(
                    ModelAttributionCandidate(
                        commit_sha=commit.sha, merged_at=commit.committed_at
                    )
                )
                continue
            merged_at = _instant(pr.merged_at) if pr.merged_at else commit.committed_at
            expired = (newest - merged_at).days > retention_days
            record, run = (
                (None, None) if expired else self._evidence(repo, pr, merged_at)
            )
            found.append(
                ModelAttributionCandidate(
                    commit_sha=commit.sha,
                    pr_number=pr.number,
                    merged_at=merged_at,
                    record=record,
                    run=run,
                )
            )
        return tuple(found)

    def _evidence(
        self, repo: str, pr: ModelPrHead, merged_at: AwareDatetime
    ) -> tuple[ModelCandidateRecord | None, ModelCandidateRun | None]:
        """The pull request's authoritative run (the latest attempt that completed before the merge) and its record."""
        runs = self._runs(
            repo, self._policy.pr_workflow_file, self._policy.pr_event, pr.head_oid
        ).runs
        best: (
            tuple[
                ModelWorkflowRunSummary,
                int,
                AwareDatetime,
                bool,
                dict[int, EnumShardConclusion],
            ]
            | None
        ) = None
        for run in runs:
            jobs = self._read(
                ModelGithubGatewayRequest(
                    operation=EnumGithubGatewayOperation.RUN_JOBS,
                    repo=repo,
                    run_id=run.id,
                    limit=MAX_LIST_LIMIT,
                ),
                ModelRunJobsResult,
            )
            if jobs.truncated:
                raise GatewayReadError(f"the job list of run {run.id} is cut")
            for attempt in sorted({job.run_attempt for job in jobs.jobs}):
                attempt_jobs = [j for j in jobs.jobs if j.run_attempt == attempt]
                if any(
                    j.status != "completed" or not j.completed_at for j in attempt_jobs
                ):
                    continue
                finished = max(
                    _instant(j.completed_at) for j in attempt_jobs if j.completed_at
                )
                shards = {
                    int(m.group(1)): _conclusion(j)
                    for j in attempt_jobs
                    if (m := self._shard_job.match(j.name))
                }
                cancelled = any(j.conclusion == "cancelled" for j in attempt_jobs)
                # Latest attempt that completed before the merge; else the earliest one after it
                # (the pure rules class it finished_after_merge, which is no evidence).
                before = finished <= merged_at
                if best is None:
                    best = (run, attempt, finished, cancelled, shards)
                    continue
                best_before = best[2] <= merged_at
                if (before and not best_before) or (
                    before == best_before
                    and (finished > best[2] if before else finished < best[2])
                ):
                    best = (run, attempt, finished, cancelled, shards)
        if best is None:
            return None, None
        run, attempt, finished, cancelled, shards = best
        record = self._record(repo, pr, run, attempt)
        return record, ModelCandidateRun(
            run_id=run.id,
            attempt=attempt,
            # The gateway cannot read a run's checkout, so the pure rules' checkout comparison has nothing to
            # compare from this adapter. The record is bound to the run by the run id, the attempt, the head
            # and the pull request it names (_record); a record that does not bind is dropped, which the
            # rules class as missing_record, never as a pass.
            checkout_sha=record.checkout_sha if record is not None else "unbound",
            completed_at=finished,
            cancelled=cancelled,
            shard_conclusions=shards,
        )

    def _record(
        self, repo: str, pr: ModelPrHead, run: ModelWorkflowRunSummary, attempt: int
    ) -> ModelCandidateRecord | None:
        name = f"{self._policy.selection_artifact_prefix}{pr.number}-{run.id}-{attempt}"
        listing = self._artifacts(repo, run.id)
        artifact = next(
            (a for a in listing.artifacts if a.name == name and not a.expired), None
        )
        if artifact is None:
            return None
        try:
            (document,) = self._archive_member(
                repo, run.id, artifact.id, _RECORD_MEMBER
            )
            record = _Record.model_validate(json.loads(document))
        except (GatewayReadError, ValueError, ValidationError):
            return None
        if (
            record.run_id != run.id
            or record.run_attempt != attempt
            or record.head_sha != pr.head_oid
            or record.pr_number != pr.number
        ):
            return None
        return ModelCandidateRecord(
            checkout_sha=record.checkout_sha,
            executed=ModelCandidateSelection(
                selected_paths=record.executed_selection.selected_paths,
                split_count=record.executed_selection.split_count,
            ),
            by_stage={
                stage: ModelCandidateSelection(
                    selected_paths=selection.selected_paths,
                    split_count=selection.split_count,
                )
                for stage, selection in record.selection_by_stage.items()
            },
        )


__all__: list[str] = ["GatewayNightlyGateway"]
