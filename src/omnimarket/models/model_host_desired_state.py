# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Desired host installations for one fleet generation and sequence."""

import re
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from omnimarket.models.model_host_wire import (
    HostSurface,
    ModelHostWire,
    NonEmptyStr,
    UtcStamp,
)

FLEET_KEY = "fleet"


class ModelHostDesiredEntry(ModelHostWire):
    """One host surface pinned to a repository tag and its resolved commit."""

    host: NonEmptyStr
    surface: HostSurface
    repository: Annotated[
        str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ]
    ref: str
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

    @field_validator("ref")
    @classmethod
    def _ref_must_be_tag(cls, value: str) -> str:
        """Accept explicit tag refs or version tags, never branches or bare commits."""
        if ".." in value:
            raise ValueError("tag ref must not contain '..'")
        if value.startswith("refs/tags/"):
            name = value.removeprefix("refs/tags/")
            if (
                not name
                or name.startswith("/")
                or name.endswith(("/", "."))
                or "//" in name
                or "@{" in name
                or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name)
                or any(
                    part.startswith(".") or part.endswith(".lock")
                    for part in name.split("/")
                )
            ):
                raise ValueError("ref must name a valid tag")
            return value
        if re.fullmatch(r"[0-9A-Fa-f]{7,64}", value):
            raise ValueError("ref must be a tag, not a bare commit id")
        if not re.fullmatch(r"v?\d+\.\d+\.\d+([.+-][0-9A-Za-z.+-]+)?", value):
            raise ValueError("ref must be a version tag or refs/tags/<name>")
        return value


class ModelHostDesiredState(ModelHostWire):
    """The complete desired installation record for a fleet generation."""

    generation: int = Field(ge=1)
    sequence: int = Field(ge=0)
    published_at: UtcStamp
    publisher: NonEmptyStr
    hosts: tuple[NonEmptyStr, ...]
    entries: tuple[ModelHostDesiredEntry, ...]

    @model_validator(mode="after")
    def _hosts_and_surfaces_are_unique(self) -> Self:
        """Require unique hosts and one declared installation per host surface."""
        hosts = set(self.hosts)
        if len(hosts) != len(self.hosts):
            raise ValueError("hosts must not repeat")
        pairs: set[tuple[str, str]] = set()
        for entry in self.entries:
            if entry.host not in hosts:
                raise ValueError(f"entry host {entry.host!r} must be in hosts")
            pair = (entry.host, entry.surface)
            if pair in pairs:
                raise ValueError(f"host and surface pair {pair!r} must not repeat")
            pairs.add(pair)
        return self

    @property
    def key(self) -> str:
        """Return the single fleet record key."""
        return FLEET_KEY

    @property
    def fold_key(self) -> tuple[int, int]:
        """Order desired state records by generation and then sequence."""
        return self.generation, self.sequence
