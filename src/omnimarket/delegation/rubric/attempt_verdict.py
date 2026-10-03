# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record class rubric evidence and apply configured measured-class refusals.

The I/O boundary around node_delegation_rubric_check_compute for the two
producers of attempt records: the bus workflow and the bus-less local port.
Recording faults are themselves recorded, as an
UNDETERMINED verdict naming ``rubric_check_error``.
"""

import logging
from functools import lru_cache

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelDelegationClassRubrics,
    ModelRubricCheckRequest,
    ModelRubricVerdict,
)

logger = logging.getLogger(__name__)

RUBRIC_CHECK_ERROR = "rubric_check_error"
_UNAVAILABLE_VERSION = "unavailable"


@lru_cache(maxsize=1)
def _load_contract() -> ModelDelegationClassRubrics:
    return load_delegation_class_rubrics()


def attempt_verdict_from(verdict: ModelRubricVerdict) -> ModelAttemptRubricVerdict:
    """The wire record of one compute verdict."""
    return ModelAttemptRubricVerdict(
        rubric_version=verdict.rubric_version,
        task_class=verdict.task_class,
        outcome=verdict.outcome.value,
        failed_criteria=verdict.failed_criteria,
        undetermined_criteria=tuple(
            row.criterion_id
            for row in verdict.criteria
            if row.outcome == EnumRubricOutcome.UNDETERMINED
        ),
    )


def rubric_check_error_verdict(
    task_class: str, rubric_version: str = _UNAVAILABLE_VERSION
) -> ModelAttemptRubricVerdict:
    """The recorded verdict of an attempt on which the compute could not run."""
    return ModelAttemptRubricVerdict(
        rubric_version=rubric_version or _UNAVAILABLE_VERSION,
        task_class=task_class or "unknown",
        outcome="UNDETERMINED",
        undetermined_criteria=(RUBRIC_CHECK_ERROR,),
    )


def record_attempt_rubric_verdict(
    *, task_class: str, request_text: str, answer_text: str
) -> ModelAttemptRubricVerdict:
    """Compute the attempt's verdict; a failure is recorded, never raised."""
    rubric_version = _UNAVAILABLE_VERSION
    try:
        contract = _load_contract()
        rubric_version = contract.rubric_version
        request = ModelRubricCheckRequest(
            task_class=task_class,
            request_text=request_text,
            answer_text=answer_text,
            rubric=contract.for_class(task_class),
        )
        return attempt_verdict_from(HandlerDelegationRubricCheck().handle(request))
    except Exception as exc:
        # The type only: the prompt and answer never reach a log line.
        logger.warning("Rubric check failed: %s", type(exc).__name__)
        return rubric_check_error_verdict(task_class, rubric_version)


def apply_measured_rubric(
    result: ModelQualityGateResult,
    verdict: ModelAttemptRubricVerdict,
    *,
    task_class: str,
) -> ModelQualityGateResult:
    """Only a configured MET class can turn recorded rubric evidence into refusal.

    Preserve an existing floor refusal. An undetermined rubric on a measured
    class cannot establish a pass and names its unavailable criteria too.
    """
    if not result.passed:
        return result
    try:
        contract = _load_contract()
    except Exception as exc:
        # An unavailable config establishes no measured class. Recording above
        # still carries the error verdict; dev acceptance retains its floor.
        logger.warning("Rubric acceptance config unavailable: %s", type(exc).__name__)
        return result
    if (
        contract.false_pass_status.get(task_class) is not EnumRangeVerdict.MET
        or verdict.task_class != task_class
        or verdict.rubric_version != contract.rubric_version
        or verdict.outcome == "PASS"
    ):
        return result
    criteria = verdict.failed_criteria + verdict.undetermined_criteria
    return result.model_copy(
        update={
            "passed": False,
            "fail_category": "rubric_failed",
            "failure_reasons": tuple(
                f"rubric_failed: {criterion}" for criterion in criteria
            ),
            "fallback_recommended": True,
        }
    )
