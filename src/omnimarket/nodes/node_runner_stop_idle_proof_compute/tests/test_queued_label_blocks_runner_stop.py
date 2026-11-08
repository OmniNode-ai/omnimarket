# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A queued job naming a stop-set label makes the idle proof fail (OMN-22952)."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers import (
    HandlerRunnerStopIdleProof,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers.handler_runner_stop_idle_proof import (
    SUBJECT_QUEUED_STOP_SET_LABEL,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    ModelProofQueuedJob,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.tests.support import (
    GAP_SECONDS,
    STOP_LABEL,
    idle_read,
    request,
)

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING


def test_queued_label_blocks_the_proof_on_the_first_read() -> None:
    queued = idle_read(
        0, queued_jobs=(ModelProofQueuedJob(job_id=42, labels=("x", STOP_LABEL)),)
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((queued, idle_read(GAP_SECONDS)))
    )
    assert result.verdict is _N
    assert result.blocking_job_ids == (42,)
    names = {s.name: s.verdict for s in result.subjects}
    assert names[SUBJECT_QUEUED_STOP_SET_LABEL] is _N


def test_queued_label_blocks_the_proof_on_the_second_read() -> None:
    queued = idle_read(
        GAP_SECONDS, queued_jobs=(ModelProofQueuedJob(job_id=7, labels=(STOP_LABEL,)),)
    )
    result = HandlerRunnerStopIdleProof().handle(request((idle_read(0), queued)))
    assert result.verdict is _N
    assert result.blocking_job_ids == (7,)


def test_queued_label_blocks_with_the_label_in_another_case() -> None:
    queued = idle_read(
        0,
        queued_jobs=(ModelProofQueuedJob(job_id=9, labels=(STOP_LABEL.upper(),)),),
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((queued, idle_read(GAP_SECONDS)))
    )
    assert result.verdict is _N


def test_queued_label_blocks_names_every_blocking_job_once() -> None:
    queued = idle_read(
        0,
        queued_jobs=(
            ModelProofQueuedJob(job_id=5, labels=(STOP_LABEL,)),
            ModelProofQueuedJob(job_id=3, labels=("stop-label-b",)),
        ),
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((queued, idle_read(GAP_SECONDS)))
    )
    assert result.blocking_job_ids == (3, 5)


def test_queued_label_blocks_not_for_a_job_with_other_labels() -> None:
    other = idle_read(
        0, queued_jobs=(ModelProofQueuedJob(job_id=1, labels=("self-hosted",)),)
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((other, idle_read(GAP_SECONDS)))
    )
    assert result.verdict is _W
    assert result.blocking_job_ids == ()
