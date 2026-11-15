# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_local_config_drift_compute: every host's declaration, none in code.

Found at ``<root>/node_host_local_config_drift_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it, and a missing overlay
refuses the run, so no host name, alias or path is ever spelled in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    ModelLocalConfigDeclaration,
)

NODE_NAME = "node_host_local_config_drift_compute"


class ModelHostLocalConfigDriftOverlay(BaseModel):
    """The declarations of every host whose local configuration the read judges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One declaration per host: its aliases, safe directories, local keys and env directories.
    hosts: tuple[ModelLocalConfigDeclaration, ...] = Field(min_length=1)
    #: The fault injected on purpose that must turn the read NOT_WORKING.
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_declaration_per_host(self) -> ModelHostLocalConfigDriftOverlay:
        names = [declaration.host for declaration in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a host is declared twice")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostLocalConfigDriftOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostLocalConfigDriftOverlay, environ)


def declaration_for(
    overlay: ModelHostLocalConfigDriftOverlay, host: str
) -> ModelLocalConfigDeclaration:
    """The declaration of one host; a host the overlay does not declare is refused naming those it does."""
    for declaration in overlay.hosts:
        if declaration.host == host:
            return declaration
    declared = ", ".join(item.host for item in overlay.hosts)
    raise KeyError(
        f"host {host} is not declared by the overlay; declared hosts: {declared}"
    )


__all__ = [
    "NODE_NAME",
    "ModelHostLocalConfigDriftOverlay",
    "declaration_for",
    "load_overlay",
]
