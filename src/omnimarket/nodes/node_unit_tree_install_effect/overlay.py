# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_unit_tree_install_effect: every setting of the unit tree install, none in code.

Found at ``<root>/node_unit_tree_install_effect/overlay.yaml`` under a root named in the environment variable
named by ``omni_root_env``. The package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a
default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_unit_tree_install_effect"


class ModelUnitTreeRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    #: The clone's path under the root named by ``omni_root_env``.
    clone_relative: str = Field(min_length=1)
    #: The revision to install when the request names none.
    ref: str = Field(min_length=1)
    #: The installed tree's path under the root.
    install_relative: str = Field(min_length=1)
    #: The install record's path under the root.
    record_relative: str = Field(min_length=1)
    #: Top-level paths of the installed tree kept across an install (e.g. ``.venv``); each relative, no ``..``, no leading ``/``.
    preserve_paths: tuple[str, ...] = ()
    #: argv run in the new tree after the swap; ``{install_path}`` is replaced by the tree's absolute path.
    sync_command: tuple[str, ...] = Field(min_length=1)
    #: Each argv run with the new tree as working directory after sync; exit 0 = ok.
    smoke_commands: tuple[tuple[str, ...], ...] = Field(min_length=1)
    command_timeout_seconds: int = Field(gt=0)
    #: How many rollback/failed trees stay.
    keep: int = Field(ge=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelUnitTreeRow:
        for path in (
            *self.preserve_paths,
            self.clone_relative,
            self.install_relative,
            self.record_relative,
        ):
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError(
                    f"path {path!r} must be relative, no '..', no leading '/'"
                )
        if self.record_relative.startswith(self.install_relative.rstrip("/") + "/"):
            raise ValueError("the install record must sit outside the installed tree")
        return self


class ModelUnitTreeInstallOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The environment variable naming the root.
    omni_root_env: str = Field(min_length=1)
    units: tuple[ModelUnitTreeRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelUnitTreeInstallOverlay:
        names = [unit.name for unit in self.units]
        if len(names) != len(set(names)):
            raise ValueError("a unit name is declared twice")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelUnitTreeInstallOverlay:
    return load_structured_overlay(NODE_NAME, ModelUnitTreeInstallOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelUnitTreeInstallOverlay",
    "ModelUnitTreeRow",
    "load_overlay",
]
