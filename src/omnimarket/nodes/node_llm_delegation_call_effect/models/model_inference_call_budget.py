# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18852: the ceiling one provider call may occupy, read from the contract.

``HandlerInferenceIntent`` runs inside a consumer whose topic
(``onex.cmd.omnibase-infra.delegation-inference-request.v1``) has ONE partition
and ONE group member, and the handler awaits the provider HTTP call inline
inside it. Every LLM call on the lane -- local and cloud, every tier -- is
therefore serialised through that one slot. Measured across 15 consecutive
calls on 2026-09-19 spanning 8 correlations and 3 providers: zero overlapping
pairs.

That makes the duration of a single rung a GLOBAL resource, not a local one. A
rung permitted to run for 300 s does not merely fail its own caller late; it
denies the slot to every other caller for 300 s. Two such gaps in a 25-minute
window produced 600 s of slot holding nothing.

The boundary therefore clamps the requested timeout to a ceiling this node's
contract ALREADY declares -- ``io_operations`` ``http_request``
``timeout_seconds`` -- rather than honouring whatever the wire carries. The
value is not invented here: the contract has declared 120 s since the node was
written, and the handler simply never read it.

**Why the clamp is here and not only at the producer.** The producer
(``_inference_timeout_seconds`` in ``node_delegation_orchestrator``) is in this
repo and could be changed, but a producer-side fix bounds ONE producer.
``ModelInferenceIntent.timeout_seconds`` accepts anything up to ``le=600.0``
from ANY publisher onto a public command topic, and the effect boundary is the
party that holds the slot. A bound enforced where the resource is consumed
holds for every producer, including ones written later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_IO_OPERATIONS_KEY = "io_operations"
_HTTP_OPERATION_TYPE = "http_request"
_TIMEOUT_KEY = "timeout_seconds"

# ``ModelInferenceIntent.timeout_seconds`` is bounded ``le=600.0`` in
# omnibase_core. A ceiling at or above that admits every value the wire can
# carry, which is a bound that does not bind -- the OMN-15504 lesson applied to
# the rung instead of the handler.
WIRE_TIMEOUT_CEILING_SECONDS: Final[float] = 600.0

# The single token a timed-out rung is grepped by, in one pass over
# ``docker logs omninode-runtime-effects``. Lives beside the budget so the
# emitter and the greps that look for it cannot drift apart.
INFERENCE_TIMEOUT_LOG_TOKEN: Final[str] = "INFERENCE_CALL_TIMEOUT"


class ModelInferenceCallBudget(BaseModel):
    """Contract-derived wall-clock ceiling for one outbound provider call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_inference_duration_seconds: int = Field(ge=1, le=3600)


def load_inference_call_budget(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelInferenceCallBudget:
    """Load the provider-call ceiling from contract.yaml, refusing an inert one.

    Raises ``ValueError`` when the contract declares no ``http_request``
    operation, when that operation declares no timeout, or when the declared
    value is at or above the wire model's own maximum. Each of those is a
    ceiling that would not clamp, and a ceiling that does not clamp reads
    downstream exactly like one that does.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    operations = raw.get(_IO_OPERATIONS_KEY)
    if not isinstance(operations, list):
        raise ValueError(f"{contract_path} missing {_IO_OPERATIONS_KEY} list")

    declared: object = None
    for operation in operations:
        if (
            isinstance(operation, dict)
            and operation.get("operation_type") == _HTTP_OPERATION_TYPE
        ):
            declared = operation.get(_TIMEOUT_KEY)
            break
    else:
        raise ValueError(
            f"{contract_path} declares no {_IO_OPERATIONS_KEY} entry of "
            f"operation_type {_HTTP_OPERATION_TYPE!r}; the inference boundary "
            "has no ceiling to clamp the requested timeout against"
        )

    if declared is None:
        raise ValueError(
            f"{contract_path} {_HTTP_OPERATION_TYPE} operation declares no "
            f"{_TIMEOUT_KEY}"
        )

    budget = ModelInferenceCallBudget(max_inference_duration_seconds=int(declared))

    if budget.max_inference_duration_seconds >= WIRE_TIMEOUT_CEILING_SECONDS:
        raise ValueError(
            f"{_IO_OPERATIONS_KEY}.{_HTTP_OPERATION_TYPE}.{_TIMEOUT_KEY} "
            f"({budget.max_inference_duration_seconds}s) must be strictly less "
            f"than the wire model's own maximum "
            f"({WIRE_TIMEOUT_CEILING_SECONDS}s); a ceiling that admits every "
            "value the wire can carry clamps nothing (OMN-18852)"
        )

    return budget


__all__ = [
    "INFERENCE_TIMEOUT_LOG_TOKEN",
    "WIRE_TIMEOUT_CEILING_SECONDS",
    "ModelInferenceCallBudget",
    "load_inference_call_budget",
]
