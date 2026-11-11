# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The messages the code eval trial orchestrator consumes (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/models/model_pr_landing_ingress.py``.
Every route's input is a model that already exists on the bus; this module only
says which run (the state_io key) each one belongs to.

* run request, scored event, nudge: ``correlation_id``.
* unit receipt: ``lane``, which the orchestrator sets to the run id on every unit.
* unit failure terminal: ``correlation_id`` when the command's id was echoed
  (E1's work unit change), else ``lane``.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScored,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_nudge import (
    ModelCodeEvalRunNudge,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_request import (
    ModelCodeEvalRunRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
)

CodeEvalOrchestratorInput = (
    ModelCodeEvalRunRequest
    | ModelLabWorkUnitReceipt
    | ModelLabWorkCommandFailure
    | ModelCodeScored
    | ModelCodeEvalRunNudge
    | ModelHostCapacityAdvertisement
    | ModelCodeEvalRunClosed
    | ModelRuntimeTick
)


def run_id_of(request: object) -> UUID | None:
    """The run a message belongs to, or None when it names none (capacity, tick)."""
    if isinstance(request, ModelLabWorkUnitReceipt):
        return _uuid(request.lane)
    if isinstance(request, ModelLabWorkCommandFailure):
        return _uuid(request.correlation_id) or _uuid(request.lane)
    if isinstance(
        request,
        ModelCodeEvalRunRequest
        | ModelCodeScored
        | ModelCodeEvalRunNudge
        | ModelCodeEvalRunClosed,
    ):
        return request.correlation_id
    return None


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


__all__ = ["CodeEvalOrchestratorInput", "run_id_of"]
