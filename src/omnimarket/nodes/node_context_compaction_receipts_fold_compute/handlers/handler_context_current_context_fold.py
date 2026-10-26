# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, order-independent fold of session boundary chains and observations."""

from __future__ import annotations

from itertools import pairwise

from omnimarket.models.context_compaction_fold import (
    EnumCurrentContextState,
    ModelCurrentContextBoundaryObservation,
    ModelCurrentContextProjectionRequest,
    ModelCurrentContextProjectionResult,
    ModelSessionCurrentContext,
    ModelSessionCurrentContextState,
)
from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    ModelActivationRetirement,
    ModelBoundaryReading,
    ModelInstallationFact,
)


def _boundary_observation(
    boundary: ModelBoundaryReading,
    facts: tuple[ModelInstallationFact, ...],
) -> ModelCurrentContextBoundaryObservation:
    verdicts = {
        fact.fact
        for fact in facts
        if boundary.activation_id is not None
        and fact.activation_id == boundary.activation_id
        and fact.boundary_uuid == boundary.boundary_uuid
    }
    installed: bool | None = None
    if EnumInstallFact.INSTALLED in verdicts:
        installed = True
    elif verdicts & {EnumInstallFact.INSTALL_MISMATCH, EnumInstallFact.NOT_INSTALLED}:
        installed = False
    return ModelCurrentContextBoundaryObservation(
        boundary_uuid=boundary.boundary_uuid,
        kind=boundary.kind,
        activation_id=boundary.activation_id,
        installed=installed,
    )


def _view(state: ModelSessionCurrentContextState) -> ModelSessionCurrentContext:
    if not state.chains:
        return ModelSessionCurrentContext(
            session_id=state.session_id, state=EnumCurrentContextState.NO_BOUNDARY
        )
    if len(state.chains) > 1:
        return ModelSessionCurrentContext(
            session_id=state.session_id,
            state=EnumCurrentContextState.CONFLICT,
            conflicts=("chains_diverge",),
        )

    by_boundary: dict[str, set[ModelCurrentContextBoundaryObservation]] = {}
    for observation in state.observations:
        by_boundary.setdefault(observation.boundary_uuid, set()).add(observation)
    chain = state.chains[0]
    resolved: dict[str, ModelCurrentContextBoundaryObservation] = {}
    conflicts: set[str] = set()
    for boundary_uuid in chain:
        readings = by_boundary.get(boundary_uuid, set())
        if not readings:
            conflicts.add("boundary_unobserved")
            continue
        if any(reading.kind is not EnumBoundaryKind.UNREADABLE for reading in readings):
            readings = {
                reading
                for reading in readings
                if reading.kind is not EnumBoundaryKind.UNREADABLE
            }
        if any(reading.installed is not None for reading in readings):
            readings = {
                reading for reading in readings if reading.installed is not None
            }
        if len(readings) > 1:
            conflicts.add("boundary_readings_differ")
        else:
            resolved[boundary_uuid] = next(iter(readings))
    if conflicts:
        return ModelSessionCurrentContext(
            session_id=state.session_id,
            state=EnumCurrentContextState.CONFLICT,
            conflicts=tuple(sorted(conflicts)),
        )

    newest = resolved[chain[-1]]
    return ModelSessionCurrentContext(
        session_id=state.session_id,
        state=EnumCurrentContextState.CURRENT,
        boundary_uuid=chain[-1],
        kind=newest.kind,
        activation_id=newest.activation_id if newest.installed is True else None,
        chain_boundary_uuids=chain,
        retirements=tuple(
            ModelActivationRetirement(
                activation_id=activation_id,
                installed_at_boundary_uuid=older,
                retired_at_boundary_uuid=newer,
            )
            for older, newer in pairwise(chain)
            if resolved[older].installed is True
            and (activation_id := resolved[older].activation_id) is not None
        ),
    )


class HandlerContextCurrentContextFold:
    """Join captured evidence and derive the session's current context."""

    def handle(
        self, request: ModelCurrentContextProjectionRequest
    ) -> ModelCurrentContextProjectionResult:
        previous = request.previous
        chains = set(previous.chains) if previous is not None else set()
        observations = set(previous.observations) if previous is not None else set()
        for event in request.events:
            if event.current.chain_boundary_uuids:
                chains.add(event.current.chain_boundary_uuids)
            observations.update(
                _boundary_observation(boundary, event.facts)
                for boundary in event.boundaries
            )

        # A discarded prefix stays redundant when joined with any future chain.
        prefixes = {chain[:length] for chain in chains for length in range(len(chain))}
        state = ModelSessionCurrentContextState(
            session_id=request.events[0].session_id,
            chains=tuple(
                sorted(chain for chain in chains if chain and chain not in prefixes)
            ),
            observations=tuple(
                sorted(
                    observations,
                    key=lambda observation: (
                        observation.boundary_uuid,
                        observation.kind.value,
                        str(observation.activation_id),
                        repr(observation.installed),
                    ),
                )
            ),
        )
        return ModelCurrentContextProjectionResult(
            state=state,
            current=_view(state),
            changed=previous is None or state != previous,
        )


__all__ = ["HandlerContextCurrentContextFold"]
