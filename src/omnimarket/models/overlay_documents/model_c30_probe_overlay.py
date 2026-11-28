# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of the C30 probe node (`overlay.yaml` under its node name): deployment values, none in code.

Nothing here has a default. Copied from the model of the C30 probe node on branch omn-19966-c30-c31-probe-nodes
(omnimarket#6949) so the overlay can be pinned to a merged revision ahead of the node.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelC30ProfileOverlay(_Frozen):
    kind: Literal["inmemory", "local", "tick"]
    # ``local``: the directory holding the ``onex`` the probe grades, put first on PATH for the probe's own commands.
    # ``tick``: the environment the tick itself runs in (the registry project's) is the one graded; no directory is named.
    onex_bin_dir: str | None = None
    # ``local``: the packages whose installed versions the run records, so M1 can compare them with the launch candidate.
    graded_packages: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _local_names_its_cli(self) -> ModelC30ProfileOverlay:
        if self.kind == "local" and not self.onex_bin_dir:
            raise ValueError("a local profile names onex_bin_dir")
        if self.kind == "tick" and self.onex_bin_dir:
            raise ValueError(
                "a tick profile grades its own environment and names no onex_bin_dir"
            )
        if self.kind in ("local", "tick") and not self.graded_packages:
            raise ValueError(f"a {self.kind} profile names graded_packages")
        return self


class ModelC30ProbeOverlay(_Frozen):
    """Every setting of the node; no key has a default."""

    default_profile: str = Field(min_length=1)
    profiles: dict[str, ModelC30ProfileOverlay]
    # The run history, one JSON line per run, under the tick's state directory.
    history_file: str = Field(min_length=1)
    history_keep: int = Field(gt=0)
    # The topic the run record is emitted to; it must be one the contract declares.
    result_topic: str = Field(min_length=1)

    @model_validator(mode="after")
    def _default_exists(self) -> ModelC30ProbeOverlay:
        if self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not a profile"
            )
        return self

    def profile(self, name: str | None) -> ModelC30ProfileOverlay:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(f"unknown profile {key!r}")
        return self.profiles[key]


__all__: list[str] = ["ModelC30ProbeOverlay", "ModelC30ProfileOverlay"]
