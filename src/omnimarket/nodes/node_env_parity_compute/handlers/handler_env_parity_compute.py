from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.parity.engine_env_parity import (
    env_parity_error_result,
    evaluate_env_parity,
)
from omnimarket.parity.model_env_parity import (
    ModelEnvParityComputeRequest,
    ModelEnvParityComputeResult,
    ModelEnvParityContractConfig,
)


class HandlerEnvParityCompute:
    """Contract-driven env parity checker over CALLER-SUPPLIED lane snapshots.

    This is the pure COMPUTE primitive: it never collects environment state
    itself, so any ``env_by_lane`` payload handed to it directly (tests,
    fixtures, simulations) is sample data by construction — TEST-ONLY input,
    not a live parity verdict. Live lane parity against the real runtime
    lanes is owned by ``node_env_parity_collect_effect``, which snapshots the
    lanes read-only over ssh and feeds this same engine (OMN-13925).
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        self._contract_path = contract_path or Path(__file__).resolve().parents[1] / (
            "contract.yaml"
        )

    def handle(
        self, request: ModelEnvParityComputeRequest
    ) -> ModelEnvParityComputeResult:
        config_result = self._load_contract_config()
        if isinstance(config_result, str):
            return env_parity_error_result(request, config_result)
        return evaluate_env_parity(request, config_result)

    def _load_contract_config(self) -> ModelEnvParityContractConfig | str:
        try:
            raw_contract = yaml.safe_load(
                self._contract_path.read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            return f"failed to read env parity contract: {exc}"
        if not isinstance(raw_contract, dict):
            return "env parity contract must parse to a mapping"
        raw_config = raw_contract.get("env_parity")
        if not isinstance(raw_config, dict):
            return "contract is missing env_parity config"
        try:
            return ModelEnvParityContractConfig.model_validate(raw_config)
        except ValidationError as exc:
            return "invalid env_parity contract config: " + exc.json(include_url=False)


__all__ = ["HandlerEnvParityCompute"]
