# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An outstanding check and its convergence bound."""

from pydantic import AwareDatetime

from ._base import ModelFrozen
from .enum_host_drift_verdict import EnumHostDriftVerdict


class ModelHostDriftOpenItem(ModelFrozen):
    key: str
    kind: str
    declared: str
    observed: str
    first_seen_at: AwareDatetime
    bound_seconds: int
    verdict: EnumHostDriftVerdict
