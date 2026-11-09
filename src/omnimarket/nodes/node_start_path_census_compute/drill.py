# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plant a bypassing start on a path: ask start admission for a start it must refuse and record the answer (OMN-22940).

The caller supplies the admission handle of a test instance whose gate store would refuse the piece (a stop open on it,
or no place left), so the answer is the path's own: refused in ``enforce`` mode, logged and started in ``observe``.
The observation is tagged ``drill`` and carries the test instance, so the census reads it as drill evidence only.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.model_start_admission import (
    ModelStartAdmissionRequest,
    ModelStartAdmissionResult,
)
from omnimarket.nodes.node_start_path_census_compute.models import (
    INSTANCE_TEST,
    TAG_DRILL,
    ModelDrillObservation,
)

AdmissionHandle = Callable[[ModelStartAdmissionRequest], ModelStartAdmissionResult]


def plant_bypassing_start(
    admit: AdmissionHandle,
    *,
    path_id: str,
    piece: str,
    run_key: str,
    now: float,
) -> ModelDrillObservation:
    """Send one start for ``piece`` on ``path_id`` through ``admit`` and keep the answer as a drill observation."""
    result = admit(
        ModelStartAdmissionRequest(
            correlation_id=f"{TAG_DRILL}-{run_key}-{path_id}",
            step_key=f"{TAG_DRILL}/{run_key}/{path_id}",
            path=path_id,
            piece=piece,
            reservation_id=f"{TAG_DRILL}-{run_key}-{path_id}",
            now=now,
        )
    )
    return ModelDrillObservation(
        path_id=path_id,
        tag=TAG_DRILL,
        instance=INSTANCE_TEST,
        mode=result.mode,
        outcome=result.outcome,
        proceed=result.proceed,
    )
