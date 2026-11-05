# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind source-control reads and review commands to the canonical lane bus."""

import inspect
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from importlib.resources import files
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.ci_red_triage import ModelCiRunFailedEvent
from omnimarket.models.model_agent_review_command import ModelAgentReviewRequest
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation as Operation,
)
from omnimarket.models.model_github_gateway_io import (
    ModelCiChecksResult,
    ModelGithubGatewayRequest,
    ModelJobLogTailResult,
    ModelMergedPrResult,
    ModelRunJobsResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.models.scm import EnumScmNode

from ..models.model_review_trigger import (
    ModelReviewTriggerConfig,
    ModelReviewTriggerEvidence,
)


def command_topic() -> str:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_agent_review_trigger_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    return str(contract["review_command_topic"])


class ReviewTriggerBus:
    """Each request uses the same SCM transport and envelope as the SCM CLI."""

    def __init__(
        self, config: ModelReviewTriggerConfig, environ: Mapping[str, str]
    ) -> None:
        self.config = config
        self.environ = environ

    def _open(self) -> AbstractAsyncContextManager[ProtocolLabRunBus]:
        root = self.environ.get("OMNI_HOME", "")
        if not root:
            raise ValueError("OMNI_HOME is required to resolve the declared bus lane")
        # Bind the public opener's signature as the other SCM adapters do.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        opener = cast(
            "Callable[..., AbstractAsyncContextManager[ProtocolLabRunBus]]",
            open_lab_run_bus,
        )
        return opener(
            **dict(
                zip(
                    names,
                    ("kafka", self.config.bus_lane, None, Path(root)),
                    strict=True,
                )
            )
        )

    async def read[ResultT: BaseModel](
        self, request: ModelGithubGatewayRequest, model: type[ResultT]
    ) -> ResultT:
        async with self._open() as bus:
            terminal = await ScmBusCaller(bus).call(
                node=EnumScmNode.GATEWAY,
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=self.config.timeout_seconds,
            )
        if terminal.failed:
            raise RuntimeError("source-control read failed")
        payload = terminal.payload.get("result", terminal.payload)
        result = model.model_validate(payload)
        if str(getattr(result, "repo", "")).lower() != request.repo.lower():
            raise ValueError("source-control read returned a different repository")
        return result

    async def publish(self, request: ModelAgentReviewRequest) -> None:
        topic = command_topic()
        async with self._open() as bus:
            await bus.publish(
                topic=topic,
                key=f"{request.repo}#{request.pr}#{request.head_sha}".encode(),
                value=envelope_bytes_for(
                    topic,
                    request.model_dump(mode="json"),
                    uuid5(NAMESPACE_URL, request.command_id),
                ),
            )


class ScmReviewTriggerSource:
    def __init__(self, bus: ReviewTriggerBus, config: ModelReviewTriggerConfig) -> None:
        self._bus = bus
        self._config = config

    async def read_head(self, repo: str, pr: int) -> str:
        current = await self._bus.read(
            ModelGithubGatewayRequest(
                operation=Operation.MERGED_PR, repo=repo, pr_number=pr
            ),
            ModelMergedPrResult,
        )
        if current.pr_number != pr or current.state.upper() != "OPEN" or current.merged:
            return ""
        return current.head_sha

    async def evidence(
        self, event: ModelCiRunFailedEvent
    ) -> tuple[ModelReviewTriggerEvidence, ...]:
        config = self._config
        repo = config.qualify_repo(event.repo)
        # The overlay identifies required review contexts. The gateway supplies
        # their current failures; unrelated red checks cannot start a review.
        checks = await self._bus.read(
            ModelGithubGatewayRequest(
                operation=Operation.CI_CHECKS,
                repo=repo,
                pr_number=event.pr_number,
            ),
            ModelCiChecksResult,
        )
        if checks.pr_number != event.pr_number:
            raise ValueError("source-control read returned a different pull request")
        required = set(checks.failing_contexts).intersection(
            config.required_review_checks, event.failing_checks
        )
        if not required:
            return ()
        run_ids = {run.run_id for run in event.failing_runs if run.check in required}
        runs = await self._bus.read(
            ModelGithubGatewayRequest(
                operation=Operation.WORKFLOW_RUNS,
                workflow=config.reviewer_workflow,
                repo=repo,
                head_sha=event.head_sha,
                limit=config.read_limit,
            ),
            ModelWorkflowRunsResult,
        )
        if runs.truncated:
            return ()
        latest: dict[int, ModelWorkflowRunSummary] = {}
        for run in runs.runs:
            previous = latest.get(run.workflow_id)
            if previous is None or (run.run_number, run.run_attempt) > (
                previous.run_number,
                previous.run_attempt,
            ):
                latest[run.workflow_id] = run
        evidence: list[ModelReviewTriggerEvidence] = []
        for run in latest.values():
            if (
                (bool(run_ids) and run.id not in run_ids)
                or run.head_sha != event.head_sha
                or run.event != "pull_request"
            ):
                continue
            jobs = await self._bus.read(
                ModelGithubGatewayRequest(
                    operation=Operation.RUN_JOBS,
                    repo=repo,
                    run_id=run.id,
                    limit=config.read_limit,
                ),
                ModelRunJobsResult,
            )
            if jobs.run_id != run.id:
                raise ValueError(
                    "source-control read returned a different workflow run"
                )
            if jobs.truncated:
                continue
            for job in jobs.jobs:
                if (
                    job.name not in config.reviewer_jobs
                    or job.run_attempt != run.run_attempt
                    or job.status != "completed"
                    or job.conclusion != "failure"
                ):
                    continue
                log = await self._bus.read(
                    ModelGithubGatewayRequest(
                        operation=Operation.JOB_LOG_TAIL,
                        repo=repo,
                        job_id=job.id,
                        tail_bytes=config.log_tail_bytes,
                    ),
                    ModelJobLogTailResult,
                )
                if log.job_id != job.id:
                    raise ValueError("source-control read returned a different job")
                evidence.append(
                    ModelReviewTriggerEvidence(
                        head_sha=run.head_sha, required=True, log_text=log.text
                    )
                )
        return tuple(evidence)
