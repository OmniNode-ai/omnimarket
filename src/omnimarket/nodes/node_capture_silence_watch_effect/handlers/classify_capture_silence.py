# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decision: no I/O and no clock read (OMN-21340)."""

from datetime import datetime

from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    EnumCaptureWatchState,
    ModelCaptureObservation,
    ModelCaptureSource,
    ModelCaptureWatchVerdict,
)


def classify_capture_silence(
    *,
    source: ModelCaptureSource,
    observation: ModelCaptureObservation | None,
    now: datetime,
    error: str | None = None,
) -> tuple[ModelCaptureWatchVerdict, ...]:
    """Equality is fresh, future rows have age zero, and failures never alarm."""
    if error is None:
        try:
            if now.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            observation = ModelCaptureObservation.model_validate(observation)
        except (ValueError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    if error is not None:
        return (
            ModelCaptureWatchVerdict(
                source_id=source.source_id,
                scope="source",
                actor=None,
                state=EnumCaptureWatchState.UNKNOWN,
                newest_at=None,
                age_seconds=None,
                bound_seconds=source.source_stale_seconds,
                error=error,
            ),
        )
    assert observation is not None
    newest = observation.source_newest_at
    age = max(0.0, (now - newest).total_seconds()) if newest is not None else None
    verdicts = [
        ModelCaptureWatchVerdict(
            source_id=source.source_id,
            scope="source",
            actor=None,
            state=(
                EnumCaptureWatchState.FRESH
                if age is not None and age <= source.source_stale_seconds
                else EnumCaptureWatchState.STALE
            ),
            newest_at=newest,
            age_seconds=age,
            bound_seconds=source.source_stale_seconds,
        )
    ]
    for actor in sorted(observation.actors, key=lambda item: item.actor):
        age = max(0.0, (now - actor.newest_at).total_seconds())
        verdicts.append(
            ModelCaptureWatchVerdict(
                source_id=source.source_id,
                scope="actor",
                actor=actor.actor,
                state=(
                    EnumCaptureWatchState.FRESH
                    if age <= source.actor_silence_seconds
                    else EnumCaptureWatchState.SILENT
                ),
                newest_at=actor.newest_at,
                age_seconds=age,
                bound_seconds=source.actor_silence_seconds,
            )
        )
    return tuple(verdicts)
