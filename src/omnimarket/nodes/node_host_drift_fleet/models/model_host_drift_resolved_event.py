# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolution payload retained in the fleet's bounded recent history."""

from typing import Literal

from pydantic import AwareDatetime, ConfigDict

from ._base import ModelFrozen


class ModelHostDriftResolvedEvent(ModelFrozen):
    model_config = ConfigDict(frozen=True, extra="ignore")
    host: str
    role: str
    check_id: str
    key: str
    kind: str
    declared: str
    last_observed: str
    first_seen_at: AwareDatetime
    resolved_at: AwareDatetime
    duration_seconds: int
    resolution: Literal["OBSERVED_CONVERGED", "AUTO_APPLIED"]
