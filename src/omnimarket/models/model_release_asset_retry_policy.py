# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a release download retries a gateway failure (OMN-14172).

Declared in ``configs/release_asset_retry.v1.yaml``. The delays are a tuple so
the schedule, and with it the attempt count, lives in one place.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "release_asset_retry.v1.yaml"
)


class ModelReleaseAssetRetryPolicy(BaseModel):
    """The statuses retried and the seconds slept between attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statuses: tuple[int, ...] = Field(
        min_length=1, description="HTTP statuses that are retried."
    )
    delays_seconds: tuple[float, ...] = Field(
        min_length=1,
        description="Seconds slept before each further attempt, in order.",
    )

    @property
    def attempts(self) -> int:
        """One attempt, then one more after each delay."""
        return len(self.delays_seconds) + 1


def load_release_asset_retry_policy(
    path: Path = _CONFIG_PATH,
) -> ModelReleaseAssetRetryPolicy:
    """Read the policy; a missing file or key raises, with no fallback."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelReleaseAssetRetryPolicy.model_validate(raw["release_asset_retry"])
