# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold plan's typed input and output: one transition and its stored row in, a row out."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_lab_alert_transitioned import ModelLabAlertTransitioned


class ModelLabAlertsPlanRequest(BaseModel):
    """A transition and the lab_alerts row stored under its key, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transition: ModelLabAlertTransitioned
    stored_row: dict[str, object] | None = None


class ModelLabAlertsPlanResponse(BaseModel):
    """The row to write; None when the transition is a replay or older than the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: dict[str, object] | None = None


__all__ = ["ModelLabAlertsPlanRequest", "ModelLabAlertsPlanResponse"]
