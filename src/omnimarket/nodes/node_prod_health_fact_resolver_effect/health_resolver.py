# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure prod-health classification for the Phase-1.3 resolver EFFECT (OMN-13441).

Classifies an already-collected prod-lane health probe result into the tri-state
``EnumProdHealth``. This module does ZERO I/O — it takes a structural probe
result (HTTP status + reachability) and returns the fail-closed health value. The
live probe (an HTTP GET against the prod-lane health endpoint) lives in the
handler's I/O boundary.

Fail-closed rules (DoD):
  * a CONFIRMED healthy probe (reachable + 2xx) -> ``HEALTHY``;
  * a CONFIRMED unhealthy probe (reachable + non-2xx) -> ``UNHEALTHY``;
  * an indeterminate probe (unreachable / error / timeout / no status) ->
    ``UNKNOWN`` (NOT ``UNHEALTHY``) so the gate still requires an approver grant.

``UNKNOWN`` is deliberately distinct from ``UNHEALTHY``: an attacker who merely
breaks the health probe (induces unreachability) lands on ``UNKNOWN``, which the
gate treats exactly like "grant required" — so breaking the probe cannot open the
recovery-waiver bypass.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import EnumProdHealth

# Inclusive-exclusive 2xx band: a reachable probe whose status is in [200, 300)
# is the only CONFIRMED-healthy signal. Everything reachable-but-outside is
# CONFIRMED-unhealthy; unreachable is indeterminate (UNKNOWN).
_HEALTHY_STATUS_MIN = 200
_HEALTHY_STATUS_MAX = 300


class ModelProbeResult(BaseModel):
    """Structural outcome of one prod-lane health probe (no classification yet).

    ``reachable`` is False whenever the probe could not produce a definitive HTTP
    status — connection error, timeout, DNS failure, or any transport exception.
    ``status_code`` is the HTTP status when reachable, else ``None``. ``detail``
    carries an optional human-readable note (e.g. the exception class) for audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool = Field(
        ...,
        description="Whether the probe produced a definitive HTTP status.",
    )
    status_code: int | None = Field(
        default=None,
        description="HTTP status when reachable; None for an indeterminate probe.",
    )
    detail: str | None = Field(
        default=None,
        description="Optional note (exception class / status text) for audit.",
    )


def classify_health(result: ModelProbeResult) -> EnumProdHealth:
    """Classify a probe result into the fail-closed tri-state health value.

    Pure + deterministic: identical inputs always yield the same value. An
    indeterminate probe fails closed to ``UNKNOWN`` rather than ``UNHEALTHY`` so a
    broken probe cannot induce the recovery-waiver path.
    """
    if not result.reachable or result.status_code is None:
        return EnumProdHealth.UNKNOWN
    if _HEALTHY_STATUS_MIN <= result.status_code < _HEALTHY_STATUS_MAX:
        return EnumProdHealth.HEALTHY
    return EnumProdHealth.UNHEALTHY


__all__: list[str] = [
    "ModelProbeResult",
    "classify_health",
]
