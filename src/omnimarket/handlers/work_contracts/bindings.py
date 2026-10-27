"""Read-only file binding for the lab runner; the compute handler stays pure.

The runner calls prepare_check before dispatch and finish_check after the command
exits. Evaluators must be outside the writable candidate worktree (mounted
read-only by the runner). Integrity failure is a failed attempt, never a warning.
This module does not execute commands or create worktrees.
"""

from pathlib import Path

from omnimarket.nodes.node_model_work_contract_compute.handlers.checks import (
    HandlerModelWorkContract,
)
from omnimarket.nodes.node_model_work_contract_compute.models.model_check import (
    ModelCheckRequest,
    ModelCheckResult,
    ModelCheckVerdict,
)
from omnimarket.nodes.node_model_work_contract_compute.models.model_work_contract import (
    ModelWorkContract,
)

from .loader import verify_pins


def prepare_check(
    contract: ModelWorkContract, root: Path, answer: Path
) -> tuple[str, ...]:
    verify_pins(contract, root)
    if contract.acceptance.kind == "schema":
        raise ValueError("schema checks use the pure schema evaluator")
    return tuple(
        arg.replace("{root}", str(root.resolve())).replace(
            "{answer}", str(answer.resolve())
        )
        for arg in contract.acceptance.spec.argv
    )


def finish_check(
    contract: ModelWorkContract,
    root: Path,
    *,
    exit_code: int,
    timed_out: bool = False,
    tests_collected: int = 1,
) -> ModelCheckVerdict:
    try:
        verify_pins(contract, root)
        intact = True
    except (ValueError, OSError):
        intact = False
    result = ModelCheckResult(
        contract_digest=contract.digest(),
        exit_code=exit_code,
        evaluator_intact=intact,
        timed_out=timed_out,
        tests_collected=tests_collected,
    )
    return HandlerModelWorkContract().handle(
        ModelCheckRequest(contract=contract, result=result)
    )
