# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host install state REDUCER handler (OMN-23957).

Pure fold: ``(prior record, step event) -> new record``. The reducer compares the digests itself, so a verify read whose
installed digest differs from the render digest can never leave the record verified, and a rollback read that differs
from the rollback target can never leave it rolled back. No I/O, no bus, no clock: times come from the event. Every
bound (digest length, history kept, detail cut) comes from the node overlay.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_host_install_state_reducer.models.model_host_install_state import (
    EnumHostInstallOutcome,
    EnumHostInstallState,
    EnumHostInstallStep,
    ModelHostInstallHistoryEntry,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_host_install_state_reducer.overlay import (
    ModelHostInstallStateOverlay,
    load_overlay,
)

HANDLER_ID = "host-install-state-reducer"

S = EnumHostInstallState
P = EnumHostInstallStep
OUT = EnumHostInstallOutcome

_TERMINAL = frozenset({S.RUN_OK, S.ROLLED_BACK})
_RESTARTABLE = frozenset({S.RUN_OK, S.ROLLED_BACK, S.FAILED})
#: The step each in-flight state waits for; a failed outcome at it fails the record.
_EXPECTED_STEP = {
    S.REQUESTED: P.SNAPSHOT,
    S.SNAPSHOTTED: P.INSTALL,
    S.INSTALLING: P.INSTALL,
    S.INSTALLED: P.VERIFY,
    S.VERIFIED: P.RUN,
}


class HostInstallTransitionError(ValueError):
    """A step that the record's state does not accept, or one for another host, unit or install."""


def _refuse(
    prior: ModelHostInstallRecord | None, event: ModelHostInstallStepEvent, why: str
) -> HostInstallTransitionError:
    state = prior.state.value if prior else "none"
    return HostInstallTransitionError(
        f"host {event.host} unit {event.unit} state {state}: "
        f"step {event.step.value}/{event.outcome.value} refused: {why}"
    )


def _cut(text: str, overlay: ModelHostInstallStateOverlay) -> str:
    return text[: overlay.detail_max_chars]


def _check_digests(
    prior: ModelHostInstallRecord | None,
    event: ModelHostInstallStepEvent,
    overlay: ModelHostInstallStateOverlay,
) -> None:
    for name in ("render_digest", "installed_digest", "rollback_target_digest"):
        value = getattr(event, name)
        if value and len(value) != overlay.digest_hex_length:
            raise _refuse(
                prior,
                event,
                f"{name} is not {overlay.digest_hex_length} hex characters",
            )


def _advance(
    prior: ModelHostInstallRecord,
    event: ModelHostInstallStepEvent,
    overlay: ModelHostInstallStateOverlay,
    state: EnumHostInstallState,
    detail: str,
    **update: object,
) -> ModelHostInstallRecord:
    entry = ModelHostInstallHistoryEntry(
        step=event.step,
        outcome=event.outcome,
        state_after=state,
        actor=event.actor,
        at=event.at,
        detail=_cut(detail, overlay),
    )
    history = (*prior.history, entry)[-overlay.history_limit :]
    return prior.model_copy(
        update={
            "state": state,
            "last_actor": event.actor,
            "updated_at": event.at,
            "history": history,
            **update,
        }
    )


def _start(
    event: ModelHostInstallStepEvent, overlay: ModelHostInstallStateOverlay
) -> ModelHostInstallRecord:
    entry = ModelHostInstallHistoryEntry(
        step=event.step,
        outcome=event.outcome,
        state_after=S.REQUESTED,
        actor=event.actor,
        at=event.at,
        detail=_cut(event.detail, overlay),
    )
    return ModelHostInstallRecord(
        correlation_id=event.correlation_id,
        host=event.host,
        unit=event.unit,
        state=S.REQUESTED,
        render_digest=event.render_digest,
        requested_by=event.actor,
        last_actor=event.actor,
        requested_at=event.at,
        updated_at=event.at,
        history=(entry,),
    )


def fold(
    prior: ModelHostInstallRecord | None,
    event: ModelHostInstallStepEvent,
    overlay: ModelHostInstallStateOverlay,
) -> ModelHostInstallRecord:
    """Fold one step event into the unit's install record. Pure."""
    _check_digests(prior, event, overlay)
    if prior is not None and (prior.host, prior.unit) != (event.host, event.unit):
        raise _refuse(prior, event, "the event is for another host or unit")

    if event.step is P.REQUEST:
        if event.outcome is not OUT.OK:
            raise _refuse(prior, event, "a request step is ok or refused")
        if prior is not None and prior.state not in _RESTARTABLE:
            raise _refuse(prior, event, "an install of this unit is in flight")
        if not event.render_digest:
            raise _refuse(prior, event, "a request carries the render digest")
        return _start(event, overlay)

    if prior is None:
        raise _refuse(prior, event, "no request recorded for this unit")
    if prior.correlation_id != event.correlation_id:
        raise _refuse(prior, event, "the event is for another install")
    state = prior.state
    if state in _TERMINAL:
        raise _refuse(prior, event, "the install is finished")

    if state is S.FAILED:
        return _fold_rollback(prior, event, overlay)

    if event.step is _EXPECTED_STEP[state] and event.outcome is OUT.FAILED:
        return _advance(
            prior,
            event,
            overlay,
            S.FAILED,
            event.detail,
            failure_detail=_cut(event.detail or f"{event.step.value}_failed", overlay),
        )

    if state is S.REQUESTED and (event.step, event.outcome) == (P.SNAPSHOT, OUT.OK):
        return _advance(
            prior,
            event,
            overlay,
            S.SNAPSHOTTED,
            event.detail,
            rollback_target_digest=event.rollback_target_digest,
        )
    if state is S.SNAPSHOTTED and (event.step, event.outcome) == (
        P.INSTALL,
        OUT.STARTED,
    ):
        return _advance(prior, event, overlay, S.INSTALLING, event.detail)
    if state in (S.SNAPSHOTTED, S.INSTALLING) and (event.step, event.outcome) == (
        P.INSTALL,
        OUT.OK,
    ):
        return _advance(
            prior,
            event,
            overlay,
            S.INSTALLED,
            event.detail,
            installed_digest=event.installed_digest,
        )
    if state is S.INSTALLED and (event.step, event.outcome) == (P.VERIFY, OUT.OK):
        if not event.installed_digest:
            raise _refuse(prior, event, "a verify read carries the installed digest")
        if event.installed_digest == prior.render_digest:
            return _advance(
                prior,
                event,
                overlay,
                S.VERIFIED,
                event.detail,
                installed_digest=event.installed_digest,
            )
        return _advance(
            prior,
            event,
            overlay,
            S.FAILED,
            event.detail or "digest_mismatch",
            installed_digest=event.installed_digest,
            failure_detail="digest_mismatch",
        )
    if state is S.VERIFIED and (event.step, event.outcome) == (P.RUN, OUT.OK):
        return _advance(prior, event, overlay, S.RUN_OK, event.detail)
    raise _refuse(prior, event, "the state does not accept this step")


def _fold_rollback(
    prior: ModelHostInstallRecord,
    event: ModelHostInstallStepEvent,
    overlay: ModelHostInstallStateOverlay,
) -> ModelHostInstallRecord:
    if event.step is not P.ROLLBACK or event.outcome is OUT.STARTED:
        raise _refuse(prior, event, "a failed install accepts a rollback result")
    if event.outcome is OUT.FAILED:
        return _advance(
            prior,
            event,
            overlay,
            S.FAILED,
            event.detail,
            rollback_failures=prior.rollback_failures + 1,
            failure_detail=_cut(event.detail or "rollback_failed", overlay),
        )
    target = prior.rollback_target_digest
    if target and event.installed_digest != target:
        return _advance(
            prior,
            event,
            overlay,
            S.FAILED,
            event.detail or "rollback_digest_mismatch",
            installed_digest=event.installed_digest,
            rollback_failures=prior.rollback_failures + 1,
            failure_detail="rollback_digest_mismatch",
        )
    return _advance(
        prior,
        event,
        overlay,
        S.ROLLED_BACK,
        event.detail,
        installed_digest=event.installed_digest,
    )


class HandlerHostInstallState:
    """Pure reducer: fold one install step event into the unit's install record projection."""

    def __init__(self, overlay: ModelHostInstallStateOverlay | None = None) -> None:
        self._overlay = overlay

    async def handle(
        self, request: ModelHostInstallReduceCommand
    ) -> ModelHandlerOutput[None]:
        """Fold the step and emit the new record as the projection."""
        if self._overlay is None:
            self._overlay = load_overlay()
        record = fold(request.prior, request.event, self._overlay)
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=uuid4(),
            correlation_id=request.event.correlation_id,
            handler_id=HANDLER_ID,
            projections=(record,),
        )


__all__: list[str] = [
    "HANDLER_ID",
    "HandlerHostInstallState",
    "HostInstallTransitionError",
    "fold",
]
