# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed capability record, request and result for the host capability node."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# A registry root as the probe script quotes it: no quote, backtick, space or ``$(``.
RegistryRoot = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9_$/.~-]+$")
]


class ModelHostCapability(BaseModel):
    """One PARITY-CAP line: ``verdict`` is ``ok``, ``missing`` (the host lacks it; a unit that needs it is refused) or ``unknown``
    (the probe could not judge it; never a refusal)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: str
    detail: str


class ModelHostCapabilityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: tuple[ModelHostCapability, ...]
    needs: frozenset[str]


class ModelHostCapabilityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``(item, reason)`` per capability the unit needs and the host lacks, in CAPABILITIES order.
    failures: tuple[tuple[str, str], ...]


class ModelLabHostsRegistryRoots(BaseModel):
    """The ``registry_root_candidates`` field of the ``lab_hosts`` overlay; its other fields belong to other readers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Registry roots tried in order on the probed host; ``$HOME`` expands there.
    registry_root_candidates: (
        Annotated[tuple[RegistryRoot, ...], Field(min_length=1)] | None
    ) = None
