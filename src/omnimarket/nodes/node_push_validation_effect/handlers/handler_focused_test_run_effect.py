# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFocusedTestRunEffect — one focused pytest run in a throwaway
container on a lab host (OMN-19359, task T3 of the delegated test loop).

Canonical def-B handler: ``handle(request: ModelFocusedTestRunRequest) ->
ModelFocusedTestRunReceipt``. No ``Plugin*`` base, no envelope type.

EXTEND-vs-NET-NEW. This is a third ``handler_routing`` operation on
``node_push_validation_effect``, beside ``run_push_validation`` and
``run_suite_evaluation``. It is not a variant of ``run_suite_evaluation``:
that operation evaluates a whole suite in the long-running gate-runner
container, which mounts the workspace; this one runs one node id against an
uncommitted overlay in a NEW container that mounts only a per-task worktree
and has no network, which is the sandbox a model-written test needs.

The sequence, and why each step is where it is (the TLA+ model of the loop,
plan task T1, is the reference: an infrastructure fault is never accepted, and
every container is removed or left to the next sweep):

1. Admission: at most two task containers on the host and a one-minute load
   of at most 8 (operator ruling 2026-09-23T21:52:08Z decision (1)), else
   ``host_busy`` with nothing created.
2. Orphan sweep: labelled containers and task directories older than an hour.
3. Worktree at the exact commit; a rerun of the same key is refused.
4. The content-keyed environment image.
5. Hide paths, write the overlay, apply each mutation. A mutation whose
   ``find`` does not occur exactly once is refused before any container
   starts: a control that silently mutated nothing would read as a pass.
6. The container, from the invocation builder only.
7. Teardown, always, and both checks recorded.

The receipt is ``completed`` only with a junit file and both teardown checks
true; everything else is ``infra_error`` (or ``host_busy``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_receipt import (
    MAX_JUNIT_BYTES,
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_focused_test_run_request import (
    ModelFocusedTestRunRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    DtlInvocationRefusedError,
    ModelContainerRunSpec,
    build_docker_run_argv,
    container_name,
)
from omnimarket.nodes.node_push_validation_effect.protocols.ephemeral_container_focused_run_subprocess import (
    EphemeralContainerFocusedRunSubprocess,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
    FocusedTestRunTaskExistsError,
    ModelContainerRunOutcome,
    ProtocolFocusedTestRunClient,
)

logger = logging.getLogger(__name__)

#: Operator ruling 2026-09-23T21:52:08Z decision (1): at most two at a time.
MAX_RUNNING_TASK_CONTAINERS = 2
MAX_LOAD_ONE_MINUTE = 8.0
#: The effect kills the container by name once the run's timeout plus this has passed.
WALL_CLOCK_SLACK_SECONDS = 30


class MutationDidNotApplyError(FocusedTestRunInfraError):
    """A mutation's ``find`` did not occur exactly once."""


def apply_mutation(content: str, find: str, replace: str, path: str) -> str:
    """Replace the single occurrence of ``find``; refuse zero or several."""
    count = content.count(find)
    if count != 1:
        raise MutationDidNotApplyError(
            f"mutation on {path} matched {count} times; exactly one is required"
        )
    return content.replace(find, replace, 1)


class HandlerFocusedTestRunEffect:
    """EFFECT handler: one focused test run, one throwaway container."""

    def __init__(self, client: ProtocolFocusedTestRunClient | None = None) -> None:
        self._client = client or EphemeralContainerFocusedRunSubprocess()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        logger.info(
            "focused_test_run: repo=%s commit=%s role=%s attempt=%s correlation_id=%s",
            request.repo,
            request.commit_sha,
            request.ref_role,
            request.attempt,
            request.correlation_id,
        )
        return await asyncio.to_thread(self.run_sync, request)

    def run_sync(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        started = time.monotonic()
        name = container_name(request.correlation_id, request.attempt, request.ref_role)

        def receipt(
            status: EnumFocusedTestRunStatus, detail: str, **fields: object
        ) -> ModelFocusedTestRunReceipt:
            return ModelFocusedTestRunReceipt(
                correlation_id=request.correlation_id,
                ref_role=request.ref_role,
                attempt=request.attempt,
                commit_sha=request.commit_sha,
                test_node_id=request.test_node_id,
                status=status,
                container_name=name,
                wall_ms=int((time.monotonic() - started) * 1000),
                detail=detail[:2000],
                **fields,
            )

        try:
            host = self._client.host_identity()
            task_root = self._client.task_root()
            admission = self._client.admission()
        except FocusedTestRunInfraError as exc:
            return receipt(EnumFocusedTestRunStatus.INFRA_ERROR, f"admission: {exc}")
        if (
            admission.running_task_containers >= MAX_RUNNING_TASK_CONTAINERS
            or admission.load_one_minute > MAX_LOAD_ONE_MINUTE
        ):
            return receipt(
                EnumFocusedTestRunStatus.HOST_BUSY,
                f"running={admission.running_task_containers} "
                f"load1={admission.load_one_minute}",
                host=host,
            )

        task_dir = (
            f"{task_root}/tasks/{request.correlation_id}/"
            f"{request.ref_role}-a{request.attempt}"
        )
        outcome: ModelContainerRunOutcome | None = None
        image_id = ""
        detail = ""
        prepared = False
        try:
            self._client.sweep_orphans()
            prepared = True
            self._client.prepare_worktree(request.repo, request.commit_sha, task_dir)
            image = self._client.ensure_env_image(request.repo, task_dir)
            image_id = image.image_id
            self._client.remove_paths(task_dir, request.hide_paths)
            self._client.write_files(task_dir, dict(request.overlay_files))
            mutated: dict[str, str] = {}
            for mutation in request.mutations:
                current = mutated.get(mutation.path)
                if current is None:
                    current = self._client.read_file(task_dir, mutation.path)
                if current is None:
                    raise MutationDidNotApplyError(
                        f"mutation target {mutation.path} is absent"
                    )
                mutated[mutation.path] = apply_mutation(
                    current, mutation.find, mutation.replace, mutation.path
                )
            self._client.write_files(task_dir, mutated)
            argv = build_docker_run_argv(
                ModelContainerRunSpec(
                    task_root=task_root,
                    task_dir=task_dir,
                    correlation_id=request.correlation_id,
                    attempt=request.attempt,
                    ref_role=request.ref_role,
                    image=image.tag,
                    test_node_id=request.test_node_id,
                    timeout_seconds=request.timeout_seconds,
                    docker_bin=self._client.docker_bin(),
                )
            )
            outcome = self._client.run_container(
                argv, name, task_dir, request.timeout_seconds + WALL_CLOCK_SLACK_SECONDS
            )
        except FocusedTestRunTaskExistsError as exc:
            # Someone else's directory: refuse, and do not tear it down.
            prepared = False
            return receipt(EnumFocusedTestRunStatus.INFRA_ERROR, str(exc), host=host)
        except (FocusedTestRunInfraError, DtlInvocationRefusedError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            teardown = (
                self._client.teardown(
                    request.repo, task_dir, request.correlation_id, name
                )
                if prepared
                else None
            )

        container_absent = bool(teardown and teardown.container_absent)
        worktree_absent = bool(teardown and teardown.worktree_absent)
        junit = (outcome.junit_xml or "") if outcome else ""
        truncated = len(junit.encode("utf-8")) > MAX_JUNIT_BYTES
        if truncated:
            junit = junit.encode("utf-8")[:MAX_JUNIT_BYTES].decode(
                "utf-8", errors="ignore"
            )
        fields: dict[str, object] = {
            "exit_code": outcome.exit_code if outcome else None,
            "junit_xml": junit,
            "junit_truncated": truncated,
            "image_id": image_id,
            "host": host,
            "teardown_container_absent": container_absent,
            "teardown_worktree_absent": worktree_absent,
        }
        if detail or outcome is None:
            return receipt(
                EnumFocusedTestRunStatus.INFRA_ERROR,
                detail or "the container never ran",
                **fields,
            )
        problems = []
        if outcome.killed_at_wall_clock:
            problems.append("killed at the wall clock")
        if not junit.strip():
            problems.append(f"no junit file came back (exit {outcome.exit_code})")
        if truncated:
            problems.append("junit exceeded the cap")
        if not container_absent:
            problems.append("teardown: a labelled container is still present")
        if not worktree_absent:
            problems.append("teardown: the task worktree is still present")
        if problems:
            tail = outcome.stderr_tail[-600:]
            return receipt(
                EnumFocusedTestRunStatus.INFRA_ERROR,
                "; ".join(problems) + f" | {tail}",
                **fields,
            )
        return receipt(
            EnumFocusedTestRunStatus.COMPLETED, outcome.stderr_tail[-600:], **fields
        )


__all__ = ["HandlerFocusedTestRunEffect", "MutationDidNotApplyError", "apply_mutation"]
