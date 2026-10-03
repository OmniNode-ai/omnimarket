# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B trusted routing complexity compute (OMN-18341).

Composition resolves the packaged rubric and Market catalogue authority once.
The handle entrypoint is deterministic, reads no files, and mutates no state.
Refusal diagnostics do not authorize routing; accepted responses are distinct.
"""

from __future__ import annotations

import re
from copy import deepcopy

from omnimarket.inference.task_class_authority import (
    TASK_COMPLEXITY_RUBRIC_PATH as CONTRACT_PATH,
)
from omnimarket.inference.task_class_authority import (
    load_task_class_authority,
)
from omnimarket.inference.task_class_authority import (
    load_task_complexity_rubric as load_contract,
)
from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_complexity_scoring import (
    measure_text,
    score_features,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_complexity import (
    ModelComplexityFeatures,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_routing_complexity import (
    EnumRubricInput,
    ModelFeatureProvenance,
    ModelRoutingClassification,
    ModelRoutingRefusal,
    ModelRoutingRequest,
    ModelStepMeasurement,
    ModelTrustedComplexityClassification,
)


class HandlerRoutingComplexity:
    """Deterministic, stateless compute over composition-owned contract snapshots."""

    def __init__(self) -> None:
        self._rubric = deepcopy(load_contract())
        self._authority = load_task_class_authority()
        self._step_rule = ModelStepMeasurement.model_validate(
            self._rubric["routing_measurement"]["dependent_reasoning_steps"]
        )
        self._step_pattern = re.compile(self._step_rule.pattern)

    def handle(
        self, request: ModelRoutingRequest
    ) -> ModelRoutingClassification | ModelRoutingRefusal:
        """Resolve every feature from a catalogue declaration or text measurement."""
        supplied = tuple(
            field.value
            for field in EnumRubricInput
            if field.value in request.model_fields_set
        )
        entry = self._authority.task_classes.get(request.workflow)
        if (
            entry is None
            or entry.complexity_contract is None
            or entry.routing_availability is not None
        ):
            return ModelRoutingRefusal(
                reason=(
                    "rubric_input_supplied"
                    if supplied
                    else "workflow_contract_unavailable"
                ),
                fields=supplied or ("workflow",),
            )

        text_features = measure_text(request.prompt)
        text_features["dependent_reasoning_steps"] = (
            len(self._step_pattern.findall(request.prompt)) + self._step_rule.base_steps
        )
        declared = entry.complexity_contract.model_dump()
        features = ModelComplexityFeatures(
            **text_features,
            **declared,
            measured=tuple(sorted(text_features)),
            declared=tuple(sorted(declared)),
        )
        provenance = {
            name: ModelFeatureProvenance(
                source="text_measurement",
                reference=f"{CONTRACT_PATH.name}#features.{name}",
                rule=str(self._rubric["features"][name]["how"]),
            )
            for name in text_features
        }
        provenance["dependent_reasoning_steps"] = ModelFeatureProvenance(
            source="text_measurement",
            reference=(
                f"{CONTRACT_PATH.name}#routing_measurement.dependent_reasoning_steps"
            ),
            rule=self._step_rule.how,
        )
        provenance.update(
            {
                name: ModelFeatureProvenance(
                    source="contract",
                    reference=(
                        "task_class_contracts.v1.yaml#task_classes."
                        f"{request.workflow}.complexity_contract.{name}"
                    ),
                    rule="Direct catalogue task-class declaration; no request override.",
                )
                for name in declared
            }
        )
        scored = score_features(features, self._rubric)
        classification = ModelTrustedComplexityClassification(
            **scored.model_dump(),
            workflow=request.workflow,
            provenance=provenance,
        )
        if supplied:
            return ModelRoutingRefusal(
                reason="rubric_input_supplied",
                fields=supplied,
                classification=classification,
            )
        return ModelRoutingClassification(classification=classification)
