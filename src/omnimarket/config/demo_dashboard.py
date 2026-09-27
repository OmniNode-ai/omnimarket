# SPDX-License-Identifier: MIT
"""Validated endpoint binding for the demo-readiness node contracts."""

from __future__ import annotations

import os

from pydantic import AnyHttpUrl, BaseModel, ConfigDict


class ModelDemoDashboardEndpoint(BaseModel):
    """The optional, contract-declared dashboard probe target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: AnyHttpUrl | None

    @classmethod
    def from_environment(cls) -> ModelDemoDashboardEndpoint:
        # Both demo node contracts declare this optional binding. One resolver owns
        # the environment read; neither handler supplies an implicit network target.
        raw_url = os.environ.get(
            "DEMO_DASHBOARD_URL", ""
        ).strip()  # url-authority-ok: contract-declared optional demo dashboard target resolved and validated at this boundary
        return cls(url=raw_url or None)

    @property
    def base_url(self) -> str | None:
        return str(self.url).rstrip("/") if self.url is not None else None


__all__ = ["ModelDemoDashboardEndpoint"]
