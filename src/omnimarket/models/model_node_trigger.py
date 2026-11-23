# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What runs a node whose contract subscribes to no topic: the typed trigger declarations of ``runtime_dispatch``.

A node with ``runtime_dispatch.external_trigger`` or an empty ``event_bus.subscribe_topics`` is started by something
outside the event bus. The contract names that something in one of these keys of ``runtime_dispatch``, so a reader can tell
a node a timer runs from a node nothing runs:

``host_unit``  the unit of ``node_host_installer_registry_units`` whose timer runs a module of this node.
``on_demand``  the command a person or a lane runs, and the owner who answers for it.
``callers``    the files of this repository that call the node, each one a path that exists and names the node.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

INSTALLER_REGISTRY_NODE = "node_host_installer_registry_units"
NODE_PACKAGE_PREFIX = "omnimarket.nodes."


class ModelHostUnitTrigger(BaseModel):
    """A host timer: the unit name in the host installer registry's ``units`` table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str = Field(min_length=1)

    def runs_node(self, units: Mapping[str, Mapping[str, Any]], node_name: str) -> bool:
        """True when the unit exists, declares a schedule and its module sits in this node's package."""
        declared = units.get(self.unit)
        if declared is None or not declared.get("schedule"):
            return False
        module = str(declared.get("module", ""))
        return module == f"{NODE_PACKAGE_PREFIX}{node_name}" or module.startswith(
            f"{NODE_PACKAGE_PREFIX}{node_name}."
        )


class ModelOnDemandTrigger(BaseModel):
    """A command run on demand and the owner who answers for it; both are required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(min_length=1, pattern=r"\S")
    owner: str = Field(min_length=1, pattern=r"\S")


class ModelCallerTrigger(BaseModel):
    """A file of this repository that calls the node: a repository-relative path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, pattern=r"^[^/].*")


class ModelRuntimeDispatchTriggers(BaseModel):
    """The trigger keys of a contract's ``runtime_dispatch`` block; every other key is read elsewhere."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    host_unit: ModelHostUnitTrigger | None = None
    on_demand: ModelOnDemandTrigger | None = None
    callers: tuple[ModelCallerTrigger, ...] = ()


__all__: list[str] = [
    "INSTALLER_REGISTRY_NODE",
    "NODE_PACKAGE_PREFIX",
    "ModelCallerTrigger",
    "ModelHostUnitTrigger",
    "ModelOnDemandTrigger",
    "ModelRuntimeDispatchTriggers",
]
