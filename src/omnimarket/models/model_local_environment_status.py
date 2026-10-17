# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One observation of one local service, as an observer reported it.

Native mode reports one row, the local runtime itself, when ``onex dashboard``
starts; that row also carries the store and the dashboard's access line. Compose
mode reports one row per service of the local bundle's roster on every observer
pass. The report id is derived from the report's own fields, so a redelivered
report is recognised and changes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState

# A fixed namespace, so a report id is the same wherever it is derived.
_REPORT_ID_NAMESPACE = uuid.UUID("5b0e4f7c-6f1e-4c9e-9a0e-19988e0a0001")


def local_environment_report_id(
    tenant_id: str,
    mode: EnumLocalEnvironmentMode,
    service: str,
    observed_state: EnumLocalServiceState,
    observed_at_iso: str,
) -> uuid.UUID:
    """Derive a report's id from what it says, so a replay carries the same id."""
    name = "|".join(
        (tenant_id, mode.value, service, observed_state.value, observed_at_iso)
    )
    return uuid.uuid5(_REPORT_ID_NAMESPACE, name)


class ModelLocalEnvironmentStatus(BaseModel):
    """The payload of ``local-environment-status``: one service, one moment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    mode: EnumLocalEnvironmentMode
    service: str = Field(min_length=1)
    observed_state: EnumLocalServiceState
    # A one-line reason is required: a state without one reads as unexplained.
    reason: str = Field(min_length=1)
    observed_at: AwareDatetime
    versions: dict[str, str] = Field(default_factory=dict)
    # Native-mode facts, carried by the local runtime's own row only.
    store_kind: str | None = None
    store_location: str | None = None
    access_mode: str | None = None
    token_minted_at: AwareDatetime | None = None
    rotate_hint: str | None = None
    report_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _derive_report_id(self) -> ModelLocalEnvironmentStatus:
        derived = local_environment_report_id(
            self.tenant_id,
            self.mode,
            self.service,
            self.observed_state,
            self.observed_at.astimezone(UTC).isoformat(),
        )
        if self.report_id is None:
            object.__setattr__(self, "report_id", derived)
        elif self.report_id != derived:
            raise ValueError(
                "report_id does not match the report's tenant, mode, service, "
                "observed state and time"
            )
        return self


__all__ = ["ModelLocalEnvironmentStatus", "local_environment_report_id"]
