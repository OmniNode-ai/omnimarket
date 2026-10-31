# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-side settings of the lab work unit node, read from its contract (OMN-21623).

The ``host_config`` block of ``node_lab_work_unit_effect``'s contract.yaml declares the
directories searched for executables, the longest a unit may run, the bounds on the git
calls and host probes, the remote a pushed sha is fetched from and how long a claude
login reading is reused. :func:`load_lab_work_unit_host_config` reads it through the
shared contract block loader; the host reader and the shell runner receive the result,
so no timeout, path or URL is held in their code.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.contract_block_loader import load_contract_block

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
CONFIG_KEY = "host_config"


class ModelLabWorkUnitHostConfig(BaseModel):
    """The values the host reader and the shell runner run with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable_search_path: tuple[str, ...] = Field(..., min_length=1)
    max_unit_timeout_seconds: int = Field(..., ge=1)
    git_timeout_seconds: int = Field(..., ge=1)
    git_remote_scheme: str = Field(..., pattern=r"^[a-z][a-z0-9+.-]*$")
    git_remote_host: str = Field(..., min_length=1)
    login_cache_ttl_seconds: float = Field(..., gt=0)
    login_check_timeout_seconds: int = Field(..., ge=1)
    memory_probe_timeout_seconds: int = Field(..., ge=1)

    @property
    def git_remote_base(self) -> str:
        """``<scheme>://<host>``, the base a pushed sha is fetched from."""
        return f"{self.git_remote_scheme}://{self.git_remote_host}"


def load_lab_work_unit_host_config(
    contract_path: Path | None = None,
) -> ModelLabWorkUnitHostConfig:
    """Read and validate the ``host_config`` block of the node's contract."""
    return load_contract_block(
        contract_path or CONTRACT_PATH, CONFIG_KEY, ModelLabWorkUnitHostConfig
    )


__all__ = [
    "CONFIG_KEY",
    "CONTRACT_PATH",
    "ModelLabWorkUnitHostConfig",
    "load_lab_work_unit_host_config",
]
