"""A recorded external check is bound to the exact contract, never executed here."""

from pydantic import Field

from .model_work_contract import FrozenModel, ModelWorkContract


class ModelCheckResult(FrozenModel):
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = Field(ge=-255, le=255)
    evaluator_intact: bool = True
    timed_out: bool = False
    tests_collected: int = Field(default=1, ge=0)


class ModelCheckRequest(FrozenModel):
    contract: ModelWorkContract
    result: ModelCheckResult


class ModelCheckVerdict(FrozenModel):
    contract_id: str
    passed: bool
    reason: str
