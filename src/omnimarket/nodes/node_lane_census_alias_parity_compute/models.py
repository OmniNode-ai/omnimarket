# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment inputs and exact census/alarm alias parity verdict."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Name = Annotated[str, StringConstraints(min_length=1)]


class ModelLaneCensusAliasParityRequest(BaseModel):
    """Both registries are supplied by the deployment; the package names no host.

    ``manifest_hosts`` is the manifest's ``hosts`` block projected to each row's
    ``aliases`` sequence. ``alarm_aliases`` is the alarm's host-id-to-alias map.
    Matching deliberately preserves the original check's exact string and tuple
    equality, including case, DNS suffixes and additional aliases.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_hosts: dict[Name, tuple[Name, ...]] = Field(min_length=1)
    alarm_aliases: dict[Name, Name] = Field(min_length=1)

    @field_validator("manifest_hosts")
    @classmethod
    def _host_ids_do_not_collide(
        cls, hosts: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        ids = [f"h{name.removeprefix('lab-')}" for name in hosts]
        if len(set(ids)) != len(ids):
            raise ValueError("census hosts map to the same alarm host")
        return hosts


class ModelLaneCensusAliasParityResult(BaseModel):
    """A mismatch stays a refusal, with the host ids needed to repair the data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matches: bool
    missing_from_alarm: tuple[str, ...]
    missing_from_census: tuple[str, ...]
    different_aliases: tuple[str, ...]
