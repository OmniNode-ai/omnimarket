# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared GitHub quota floor (OMN-19826).

First declared by node_pr_landing_github_effect; promoted here (OMN-20912) so
node_github_repo_write_effect reads its own contract's ``quota_floor`` block
with the same rule, without reaching into the landing node's models package
(OMN-9263).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing_github.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaReading,
)


class ModelGithubQuotaFloor(BaseModel):
    """Minimum remaining quota per resource below which a call is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["response_headers"]
    min_remaining: dict[str, int] = Field(min_length=1)

    @classmethod
    def from_contract(cls, contract_path: Path) -> ModelGithubQuotaFloor:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("quota_floor"), dict):
            raise ValueError(f"{contract_path} declares no quota_floor block")
        return cls.model_validate(raw["quota_floor"])

    def refusal(
        self, reading: ModelGithubQuotaReading
    ) -> EnumPrLandingGithubFailureReason | None:
        """Return ``QUOTA_FLOOR`` when the reading is under its resource's floor.

        A resource the contract does not declare is refused (fail closed).
        """
        floor = self.min_remaining.get(reading.resource)
        if floor is None or reading.remaining < floor:
            return EnumPrLandingGithubFailureReason.QUOTA_FLOOR
        return None
