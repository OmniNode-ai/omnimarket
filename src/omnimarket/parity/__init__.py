"""Shared env-parity types and evaluation engine (OMN-13925).

Home for the env-parity models and the pure evaluation function shared by:

- ``node_env_parity_compute`` — the pure COMPUTE primitive over
  caller-supplied snapshots (test / simulation input path), and
- ``node_env_parity_collect_effect`` — the live collection front-end that
  snapshots real runtime lanes (read-only ``ssh`` + ``docker inspect``) and
  feeds this engine.

Promoted out of ``node_env_parity_compute`` so the collect EFFECT does not
import another node's private handler/model packages.
"""

from omnimarket.parity.engine_env_parity import (
    env_parity_error_result,
    evaluate_env_parity,
)
from omnimarket.parity.model_env_parity import (
    EnumEnvParityConsistency,
    ModelEnvParityComputeRequest,
    ModelEnvParityComputeResult,
    ModelEnvParityContractConfig,
    ModelEnvParityGap,
    ModelEnvParityLaneVariableResult,
    ModelEnvParityVariableRule,
)

__all__ = [
    "EnumEnvParityConsistency",
    "ModelEnvParityComputeRequest",
    "ModelEnvParityComputeResult",
    "ModelEnvParityContractConfig",
    "ModelEnvParityGap",
    "ModelEnvParityLaneVariableResult",
    "ModelEnvParityVariableRule",
    "env_parity_error_result",
    "evaluate_env_parity",
]
