# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fills the environment names the brief check reads from the secret store when the unit's environment has none (OMN-24239)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelBriefEnvironment,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    BriefEnvironmentAbsentError,
    ProtocolStoreReader,
)


class StoreEnvironmentFiller:
    """Returns a copy of the unit's environment with the brief check's names filled from the store when absent or blank."""

    def __init__(
        self, settings: ModelBriefEnvironment, store: ProtocolStoreReader
    ) -> None:
        self._settings = settings
        self._store = store

    def fill(self, environ: Mapping[str, str]) -> dict[str, str]:
        """A copy of ``environ`` with each declared name filled; raises when a name is in neither place."""
        filled = dict(environ)
        reasons: dict[str, str] = {}
        for setting in self._settings.settings:
            if environ.get(setting.env, "").strip():
                continue
            try:
                value = asyncio.run(
                    self._store.read(self._settings.folder, setting.secret)
                )
            except Exception as exc:
                reasons[setting.env] = f"read failed: {type(exc).__name__}"
                continue
            if value is None or not value.get_secret_value().strip():
                reasons[setting.env] = "absent"
                continue
            filled[setting.env] = value.get_secret_value()
        if reasons:
            said = "; ".join(f"{name} {why}" for name, why in reasons.items())
            raise BriefEnvironmentAbsentError(
                f"{', '.join(reasons)}: not in the unit's environment and the store folder "
                f"{self._settings.folder} holds no usable entry ({said})"
            )
        return filled


__all__ = [
    "StoreEnvironmentFiller",
]
