# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Short availability checks for the models listed by one supplied endpoint."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_endpoint_models import ModelEndpointModelsRequest


class ModelEndpointProbeRequest(ModelEndpointModelsRequest):
    models: tuple[str, ...] = Field(min_length=1, max_length=256)
    total_timeout_seconds: float = Field(default=60, gt=0, le=300)

    @field_validator("models")
    @classmethod
    def safe_models(cls, models: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(models)) != len(models) or any(
            not model.strip() or any(ord(c) < 32 or ord(c) == 127 for c in model)
            for model in models
        ):
            raise ValueError("Probe models must be unique nonempty printable IDs")
        return models


class ModelEndpointProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str
    status: Literal["ready", "throttled", "unavailable", "unprobed"]
    http_status: int | None = None
    message: str = ""


class ModelEndpointProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: str
    probes: tuple[ModelEndpointProbe, ...]

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(
            item.model
            for status in ("ready", "throttled", "unprobed")
            for item in self.probes
            if item.status == status
        )

    @property
    def all_throttled(self) -> bool:
        offered = [item for item in self.probes if item.status != "unavailable"]
        return bool(offered) and all(item.status == "throttled" for item in offered)
