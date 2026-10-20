# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988 mode 1: the native install's environment report, built by `onex dashboard`.

A native install has one service to report, the local runtime this process is
part of. Its report names the installed versions, the store the dashboard
serves, how the dashboard is reached, when this start's token was minted and
how to rotate it. The token itself is never part of it.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

import yaml

from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    DashboardBundleError,
    load_pin,
)

NATIVE_SERVICE = "local-runtime"
ACCESS_MODE = "loopback-token"
ROTATE_HINT = (
    "Restart `onex dashboard`: every start mints a new token, and the one "
    "before it stops working."
)
_REPORTED_PACKAGES = ("omnimarket", "omnibase_core")


def heartbeat_seconds() -> float:
    """How often a serving dashboard re-reports, from its contract's config."""
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_local_dashboard_serve_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return float(contract["config"]["local_environment_report"]["heartbeat_seconds"])


def installed_versions() -> dict[str, str]:
    """The versions this install runs: its packages and the pinned pages bundle."""
    versions: dict[str, str] = {}
    for package in _REPORTED_PACKAGES:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    with contextlib.suppress(DashboardBundleError):
        versions["omnidash"] = load_pin().version
    return versions


def build_native_report(
    *,
    tenant_id: str,
    store_location: Path,
    token_minted_at: datetime,
    observed_at: datetime,
    exposure_count: int,
    versions: dict[str, str],
) -> ModelLocalEnvironmentStatus:
    """The native install's report at ``observed_at``, while the dashboard serves."""
    return ModelLocalEnvironmentStatus(
        tenant_id=tenant_id,
        mode=EnumLocalEnvironmentMode.NATIVE,
        service=NATIVE_SERVICE,
        observed_state=EnumLocalServiceState.RUNNING,
        reason=f"onex dashboard is serving {exposure_count} exposures",
        observed_at=observed_at,
        versions=versions,
        store_kind="sqlite",
        store_location=str(store_location),
        access_mode=ACCESS_MODE,
        token_minted_at=token_minted_at,
        rotate_hint=ROTATE_HINT,
    )


__all__ = [
    "ACCESS_MODE",
    "NATIVE_SERVICE",
    "ROTATE_HINT",
    "build_native_report",
    "heartbeat_seconds",
    "installed_versions",
]
