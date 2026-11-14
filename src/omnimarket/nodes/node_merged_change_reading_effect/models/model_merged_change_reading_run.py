# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run request and result of the merged-change reader, and the overlay documents it reads."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_merged_change_reading import (
    ModelMcrChange,
    ModelMcrConfig,
    ModelMcrResult,
    ModelMcrRunner,
)


class ModelMcrRunRequest(BaseModel):
    """One run: the merged changes to read, an optional clock override and an optional overlay config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: tuple[ModelMcrChange, ...] = Field(min_length=1)
    now: datetime | None = None
    config: ModelMcrConfig | None = None

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("now must carry a timezone")
        return value


class ModelMcrRunResult(BaseModel):
    """The readings, and how many log lines and runners the sources supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reading: ModelMcrResult
    runners_read: int
    changes_without_sha: int


class ModelMcrStoreCheckout(BaseModel):
    """A repository whose checkout on the runtime host is what the runtime mounts (the overlay store).

    A merge in the history of that checkout's head is in the running store, so the reading's build fact for it is the
    checkout's: no provenance file records an overlay repository.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    path: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")


class ModelMcrRunnerOverlay(BaseModel):
    """The overlay document that declares how each merged change runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runners: tuple[ModelMcrRunner, ...]
    # Where the runtime serves its introspection manifest (the contracts it loaded), read from the runtime host. The
    # log's discovery lines are lost when the container's log rotates, so the manifest is the loaded-node source. Empty:
    # only the log's discovery lines are read.
    manifest_url: str = Field(default="", pattern=r"^(https?://[^\s]+)?$")
    store_checkouts: tuple[ModelMcrStoreCheckout, ...] = ()


class ModelMcrUnitHost(BaseModel):
    """A lab host that runs units, and the environment variable that holds its ssh target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class ModelMcrUnitHostsOverlay(BaseModel):
    """The overlay document that names the lab hosts units run on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelMcrUnitHost, ...]


class ModelMcrLogLabels(BaseModel):
    """How the runtime's log and the systemd unit show a node loaded, a consumer joined and an event published.

    Every pattern and format is the effect contract's, never code: a change of the runtime's log wording is a
    contract edit with a test, not a code edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filter_regex: str = Field(min_length=1)
    time_format: str = Field(min_length=1)
    loaded_pattern: str = Field(min_length=1)
    subscribed_pattern: str = Field(min_length=1)
    published_pattern: str = Field(min_length=1)
    health_pattern: str = Field(min_length=1)
    healthy_status: str = Field(min_length=1)
    started_at_template: str = Field(min_length=1)
    docker_timeout_seconds: int = Field(gt=0)
    ssh_attempts: int = Field(gt=0)
    manifest_contracts_key: str = Field(min_length=1)
    # A contract is listed under its own name and under the name of the entry point that loads it; the node map names
    # the entry point (the node's directory), so both are loaded names.
    manifest_name_keys: tuple[str, ...] = Field(min_length=1)
    manifest_fetch_options: str = Field(min_length=1)
    unit_show_options: str = Field(min_length=1)
    unit_show_properties: str = Field(min_length=1)
    unit_time_format: str = Field(min_length=1)
    unit_result_template: str = Field(min_length=1)
    unit_ok_result: str = Field(min_length=1)
    merge_branches: tuple[str, ...] = Field(min_length=1)
    merge_subject_pattern: str = Field(min_length=1)
    git_timeout_seconds: int = Field(gt=0)
    store_source: str = Field(min_length=1)
