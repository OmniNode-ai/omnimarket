# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts and verdict for the host-drift plugin revision decision (OMN-20689)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPluginDriftVerdict(StrEnum):
    OK = "OK"
    DRIFT = "DRIFT"


class ModelPluginSyncRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match: str
    kinds: tuple[str, ...]
    observed_contains: tuple[str, ...] = ()
    sync_class: str
    action: str | None = None
    alert_only_observed: tuple[str, ...] = ()
    # when true the rule matches only when an installed version differs from the merged version (OMN-24404)
    version_differs: bool = False
    between_lanes: bool = False


class ModelPluginProbeFacts(BaseModel):
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


class ModelPluginRevisionFacts(ModelPluginProbeFacts):
    """The compute's whole input: the probe facts and the plugin sync rules to route them through.

    The rules are read from their overlay by node_host_drift_plugin_rules_effect and handed in by
    node_host_drift_plugin_revision_orchestrator; this node never reads a file (OMN-20689).
    """

    rules: tuple[ModelPluginSyncRule, ...] = Field(
        description="the plugin sync rules in first-match order; none routes every drift to alert only"
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
