# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the run engine node and the tool that holds the order of work.

This protocol is the shape the node needs. The existing ``ProtocolWorkflowOrchestrator`` and
``ProtocolLiteralWorkflowStateStore`` in omnibase_spi return neither an accepted, duplicate or unknown start result,
a release outcome nor a cancel outcome, so those three are the gap the spi protocols need closed; until they carry it
this protocol holds it here, and the spi adapters in this package (``adapter_spi_orchestrator`` and
``adapter_spi_state_store``) present the existing protocols over it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunStatus,
    EnumRunStepKind,
    EnumStartOutcome,
    ModelRunSpec,
    ModelRunState,
    ModelRunStepRecord,
)


class RunEngineBackendUnavailableError(RuntimeError):
    """The backend could not be reached or answered no usable answer. Callers fail closed on it."""


class RunEngineUnsupportedError(RuntimeError):
    """The backend has no meaning for this operation; the refusal names the operation."""


@runtime_checkable
class ProtocolRunEngineBackend(Protocol):
    """A tool that sequences steps and holds their order and waits across process death."""

    @property
    def name(self) -> str:
        """The overlay value that selects this backend."""
        ...

    @property
    def durable(self) -> bool:
        """Whether a run survives the death of the engine process."""
        ...

    async def start_run(self, spec: ModelRunSpec) -> EnumStartOutcome: ...

    async def release_run(
        self, run_key: str, condition: str, release_id: str
    ) -> EnumReleaseOutcome: ...

    async def cancel_run(self, run_key: str) -> EnumCancelOutcome: ...

    async def get_run_state(self, run_key: str) -> ModelRunState: ...

    async def settle(self, run_key: str, timeout_seconds: float) -> ModelRunState:
        """Return once the run waits, ends or the timeout passes; the state read last."""
        ...

    async def health(self) -> bool: ...


def decide_release(
    state: ModelRunState, condition: str, release_id: str
) -> EnumReleaseOutcome | None:
    """The release outcome that needs no send, or ``None`` when the release is to be sent.

    A release is a duplicate when the wait it names was already released by the same id, stale when the wait was
    released by another id or is not the run's current wait, and a finished run answers ``run_finished``.
    """
    if state.status is EnumRunStatus.NOT_FOUND:
        return EnumReleaseOutcome.UNKNOWN_RUN
    record = _wait_record(state.recorded, condition)
    if record is not None:
        return (
            EnumReleaseOutcome.DUPLICATE
            if record.released_by == release_id
            else EnumReleaseOutcome.STALE
        )
    if state.status in (
        EnumRunStatus.COMPLETED,
        EnumRunStatus.FAILED,
        EnumRunStatus.CANCELLED,
    ):
        return EnumReleaseOutcome.RUN_FINISHED
    if state.status is EnumRunStatus.WAITING and state.waiting_on == condition:
        return None
    return EnumReleaseOutcome.STALE


def _wait_record(
    recorded: tuple[ModelRunStepRecord, ...], condition: str
) -> ModelRunStepRecord | None:
    for record in recorded:
        if (
            record.kind is EnumRunStepKind.WAIT_RELEASE
            and record.condition == condition
        ):
            return record
    return None
