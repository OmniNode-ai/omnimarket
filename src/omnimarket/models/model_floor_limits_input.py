# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readings the factory floor's Limits panel is built from.

node_factory_floor_effect reads each source into these models and builds the section with
``build_limits``; they live here and not in either node's package (no cross-node model reach-in). Every
field is a reading the caller already made: a source that was not read is ``None`` (or a reading with an empty
``read_at``), never an empty tuple or a zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must carry a UTC offset")
    return value


TokenState = Literal["usable", "limited", "revoked"]


class ModelFloorLimitsConfig(BaseModel):
    """The contract's ``limits`` block: the publish cycle a reading is judged against and the API budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A reading older than this is UNKNOWN: one publish cycle of the floor record.
    publish_cycle_seconds: int = Field(gt=0)
    # The month budget of one Anthropic API key (the contract value the meter plan names).
    api_budget_usd: float = Field(gt=0)
    # A projection from under this much elapsed month is not made (one day of spend says nothing about the month).
    projection_min_elapsed_hours: int = Field(gt=0)


class ModelFloorPoolToken(BaseModel):
    """One token of a host's Claude token pool as the pool reading states it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account: str = Field(min_length=1)
    state: TokenState
    limited_until: str = ""


class ModelFloorHostTokenPool(BaseModel):
    """One lab host's token pool and when it was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    read_at: str = ""
    tokens: tuple[ModelFloorPoolToken, ...] = ()


class ModelFloorApiKeySpend(BaseModel):
    """One Anthropic API key's spend this month as the meter reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    month: str = ""
    month_spend_usd: float | None = None


class ModelFloorApiMeter(BaseModel):
    """The meter's reading of every API key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: str = ""
    keys: tuple[ModelFloorApiKeySpend, ...] = ()


class ModelFloorUsageWindow(BaseModel):
    """A usage window of a subscription engine (Codex, GLM coding plan): how much of it is used and when it resets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: str = ""
    window: str = ""
    used_pct: float | None = None
    resets_at: str = ""


class ModelFloorLabModelRung(BaseModel):
    """One local lab model rung: whether it answers and when it last passed its smoke."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    read_at: str = ""
    available: bool | None = None
    last_smoke_at: str = ""


class ModelFloorEngineReadings(BaseModel):
    """The engine readings: Codex, the GLM coding plan and the lab model rungs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    codex: ModelFloorUsageWindow | None = None
    glm: ModelFloorUsageWindow | None = None
    lab_models: tuple[ModelFloorLabModelRung, ...] = ()


class ModelFloorIdleCarry(BaseModel):
    """What the previous record said about idle slots, to accrue the day's idle slot-hours."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    day: str = ""
    idle_total: int | None = None
    idle_slot_hours: float | None = None
    unread_minutes: float = 0.0

    _utc = field_validator("generated_at")(_aware)


class ModelFloorIdleHostReading(BaseModel):
    """One host's idle slots at this record (copied from the merged-and-running count)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    state: Literal["read", "unread", "out"]
    slots: int | None = None
    idle: int | None = None


class ModelFloorIdleReading(BaseModel):
    """The idle slots now: the Eastern day (the operator's) and each host's idle count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str = Field(min_length=1)
    hosts: tuple[ModelFloorIdleHostReading, ...]
    idle_total: int
    idle_unread: int = 0


class ModelFloorLimitsInput(BaseModel):
    """Everything ``build_limits`` reads. The builder has no clock: ``now`` is a fact the caller supplies.

    A source that could not be read is ``None`` and its rows read UNKNOWN with the cause in ``causes``
    (keyed by source name).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    config: ModelFloorLimitsConfig
    cloud_accounts: tuple[ModelFloorCloudAccount, ...] | None = None
    cloud_accounts_source: str = ""
    token_pool: tuple[ModelFloorHostTokenPool, ...] | None = None
    token_pool_source: str = ""
    api_meter: ModelFloorApiMeter | None = None
    api_meter_source: str = ""
    # The key names the deployment expects a row for even when the meter did not name them.
    api_key_names: tuple[str, ...] = ()
    engines: ModelFloorEngineReadings | None = None
    engines_source: str = ""
    idle: ModelFloorIdleReading | None = None
    idle_carry: ModelFloorIdleCarry | None = None
    causes: dict[str, str] = Field(default_factory=dict)

    _utc = field_validator("now")(_aware)
