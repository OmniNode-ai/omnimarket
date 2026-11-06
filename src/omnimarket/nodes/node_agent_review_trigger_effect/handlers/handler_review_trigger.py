# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request a review when a required exact-head review produced no voter verdict.

The existing ci-run-failed stream is the seam: it carries repository, PR, head
and workflow identities, unlike a gate-accept terminal. Read the completed job
through the source-control node; neither a red check alone nor an unavailable
rung alone justifies a request. No gate, voter policy or login is changed.
"""

import asyncio
import fcntl
import hashlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
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
    ModelReviewTriggerResult,
)
from ..protocols.protocol_review_trigger import (
    ProtocolDispatchJournal,
    ProtocolReviewCommandPublisher,
    ProtocolReviewTriggerSource,
)

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


class FileDispatchJournal:
    def __init__(self, root: Path, *, poll_seconds: float) -> None:
        self._root = root
        self._poll_seconds = poll_seconds

    @asynccontextmanager
    async def lock(self, command_id: str) -> AsyncIterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        with (self._root / f"{command_id}.lock").open("a") as lock:
            # A nonblocking flock yields without holding up the runtime event loop.
            # The descriptor closes on cancellation and releases its process lock.
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(self._poll_seconds)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def sent(self, command_id: str) -> bool:
        return (self._root / command_id).is_file()

    def record(self, command_id: str) -> None:
        with (self._root / command_id).open("a") as marker:
            marker.flush()
            os.fsync(marker.fileno())
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


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
