# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The launch candidate manifest the release train publishes (OMN-23343, plan task M13).

The manifest names what the train must publish and what every proof must have installed: the three
wheels with their sha256 and RECORD, the plugin version and file list, the protected console scripts,
the dashboard bundle pin and the changes the cut must contain. All sha256 values are lowercase hex.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelManifestRecordEntry(BaseModel):
    """One wheel RECORD line: a path and its sha256 (empty for a line that lists no hash)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str = ""


class ModelManifestDistribution(BaseModel):
    """One published wheel of the candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    wheel_filename: str = Field(min_length=1)
    wheel_sha256: str = Field(min_length=64, max_length=64)
    record: tuple[ModelManifestRecordEntry, ...]
    entry_points: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Group -> entry name -> value, as the wheel's entry_points.txt declares.",
    )


class ModelManifestProtectedScript(BaseModel):
    """A console script name only its owning distribution may declare, with one target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    target: str = Field(min_length=1)


class ModelManifestPluginFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)


class ModelManifestPlugin(BaseModel):
    """The plugin identified by version and file content, never by commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    omniclaude_commit: str = Field(min_length=7)
    files: tuple[ModelManifestPluginFile, ...]


class ModelManifestDashboardBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)


class ModelManifestRequiredChange(BaseModel):
    """A merge the cut must contain: the sha must be an ancestor of the repo's candidate tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    sha: str = Field(min_length=7)


class ModelReleaseCandidateManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(min_length=1)
    distributions: tuple[ModelManifestDistribution, ...] = Field(min_length=1)
    protected_scripts: tuple[ModelManifestProtectedScript, ...] = Field(min_length=1)
    plugin: ModelManifestPlugin
    dashboard_bundle: ModelManifestDashboardBundle
    required_changes: tuple[ModelManifestRequiredChange, ...] = ()
