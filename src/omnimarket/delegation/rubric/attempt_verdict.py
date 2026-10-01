# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record the class rubric verdict on a delegation attempt; never decide with it.

The I/O boundary around node_delegation_rubric_check_compute for the two
producers of attempt records: the bus workflow and the bus-less local port.
Both call this after their accept or climb decision is settled. Nothing here
may raise into a delegation: any failure is itself recorded, as an
UNDETERMINED verdict naming ``rubric_check_error``.
"""

import logging
from functools import lru_cache

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
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
