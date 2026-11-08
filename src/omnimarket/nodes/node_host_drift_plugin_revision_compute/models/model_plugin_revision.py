# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts and verdict for the host-drift plugin revision decision (OMN-20689)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPluginDriftVerdict(StrEnum):
    OK = "OK"
    DRIFT = "DRIFT"


class ModelPluginRevisionFacts(BaseModel):
    """What the host-side probe measured about one installed plugin.

    The probe reads git and the installed record; this node only decides. A plugin is behind
    when its installed revision lacks the merged tip of its repository's ref, whatever its
    version string says (an unchanged version 0.1.30 read "latest" and never moved).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="plugin@marketplace, e.g. onex-overlays@omninode-internal",
    )
    ref: str = Field(
        min_length=1, description="the repository ref the plugin ships from"
    )
    installed_versions: tuple[str, ...] = Field(min_length=1)
    merged_version: str = Field(min_length=1)
    installed_revision: str | None = Field(
        default=None, description="gitCommitSha the installed record holds"
    )
    merged_tip: str | None = Field(
        default=None, description="40-hex tip of origin/<ref>, None when unresolvable"
    )
    tip_in_installed: bool | None = Field(
        default=None,
        description="True when the installed revision contains the tip; None when the clone cannot say",
    )
    carried_commits: int = Field(
        default=0, ge=0, description="commits installed holds that origin/<ref> lacks"
    )
    stale_reason: str = Field(
        default="", description="placement's own plugin-stale reason, or empty"
    )
    tree_drift: str = Field(
        default="",
        description="a tree-integrity drift note (modified, hotpatched); it settles the verdict alone",
    )
    tree_ok: str = Field(
        default="",
        description="the note appended when the tree equals its shipped revision",
    )


class ModelPluginRevisionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumPluginDriftVerdict
    observed: str
    behind: bool
    version_differs: bool
    sync_action: str | None = Field(
        default=None,
        description="the policy action the observed text routes to; None is alert-only",
    )
    between_lanes: bool = False


class ModelPluginSyncRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match: str
    kinds: tuple[str, ...]
    observed_contains: tuple[str, ...] = ()
    sync_class: str
    action: str | None = None
    alert_only_observed: tuple[str, ...] = ()
    between_lanes: bool = False
