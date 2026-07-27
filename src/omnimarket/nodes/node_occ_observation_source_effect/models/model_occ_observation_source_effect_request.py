# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccObservationSourceEffectRequest — read the durable OCC observation
trail and dedupe it (OMN-14888).

This is the read half that "points the dedup projection at OCC as its
source": given a local checkout directory of ``onex_change_control`` (already
cloned by the caller, or a fixture in tests), walk the append-only
``drift/occ_observations/**/*.yaml`` tree, parse each file back into a
:class:`~omnimarket.events.occ_observation_record.ModelOccObservationRecord`
(OMN-14851, unchanged), and hand the whole raw log to the EXISTING, UNMODIFIED
:func:`~omnimarket.events.occ_observation_record.project_qualifying_observations`.

``checkout_dir`` is caller-supplied (not cloned by this node) so unit/golden
tests exercise the real dedup path against a plain temp directory with zero
network or git dependency; a live orchestrator clones ``onex_change_control``
once and passes the path here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOccObservationSourceEffectRequest(BaseModel):
    """Command to load + dedupe the durable OCC observation trail from disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkout_dir: str = Field(
        ...,
        description="Local filesystem path to a onex_change_control checkout "
        "(or any directory containing a drift/occ_observations/ tree).",
    )


__all__ = ["ModelOccObservationSourceEffectRequest"]
