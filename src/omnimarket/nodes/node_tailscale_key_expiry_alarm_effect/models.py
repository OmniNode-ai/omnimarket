# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests, observations and local alarm rows for the expiry watch."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelKeyExpiryReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: AwareDatetime
    expires_at: AwareDatetime | None
    expired: bool


class ModelKeyExpiryAlarm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Literal["ALARM"] = "ALARM"
    condition: Literal["tailscale-key-expiry"] = "tailscale-key-expiry"
    subject: str
    owner: str
    raised_at: AwareDatetime
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None
    message_ref: str | None = None


class ModelKeyExpiryRequest(BaseModel):
    now: AwareDatetime
    owner: str
    readings: dict[str, ModelKeyExpiryReading]


class ModelKeyExpiryResult(BaseModel):
    alarms: tuple[ModelKeyExpiryAlarm, ...]


class ModelKeyExpiryCache(BaseModel):
    readings: dict[str, ModelKeyExpiryReading]


class ModelKeyExpiryTickRequest(BaseModel):
    now: AwareDatetime
    probe: bool = False


class ModelKeyExpiryTickResult(BaseModel):
    ran_at: AwareDatetime
    source: Literal["live", "cache", "unavailable"]
    cached_hosts: tuple[str, ...] = ()
    unread_hosts: tuple[str, ...] = ()
    due: int = 0
    raised: int = 0
    delivered: int = 0
    push_refused: tuple[str, ...] = ()
