# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay (settings), request and result of the host observability installer.

Every address, port, path, image, threshold and retention bound comes from the overlay; the code
holds none. A scraped host must be a row of the ``lab_hosts`` table, so a host is never scraped
that the lab does not declare, and a ``lab_hosts`` row with no declared target is reported.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
AbsPath = Annotated[str, StringConstraints(pattern=r"^/[^\s]*$")]
Mode = Literal["dry_run", "install", "status", "readback"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelObservabilityTarget(_Frozen):
    """One scraped host. ``exporter`` says whether this installer puts the exporter there or only scrapes one that is."""

    address: NonEmptyStr
    exporter: Literal["install", "existing"]
    port: int = Field(gt=0, lt=65536)


class ModelObservabilityContainer(_Frozen):
    """The image, container name, address and published port of one container this installer keeps running."""

    image: NonEmptyStr
    container: NonEmptyStr
    unit: NonEmptyStr
    address: NonEmptyStr
    port: int = Field(gt=0, lt=65536)
    network: NonEmptyStr
    memory_gib: int = Field(gt=0)
    memory_swap_gib: int = Field(gt=0)

    @model_validator(mode="after")
    def _swap_not_below_memory(self) -> ModelObservabilityContainer:
        if self.memory_swap_gib < self.memory_gib:
            raise ValueError(
                "memory_swap_gib is memory plus swap, so it cannot be below memory_gib"
            )
        return self


class ModelObservabilityExporter(_Frozen):
    """The host exporter container: host network, the host's filesystem read-only, the collectors it enables."""

    image: NonEmptyStr
    container: NonEmptyStr
    unit: NonEmptyStr
    rootfs_mount: AbsPath
    textfile_directory: AbsPath
    enable_collectors: tuple[NonEmptyStr, ...] = ()
    network: NonEmptyStr
    memory_gib: int = Field(gt=0)
    memory_swap_gib: int = Field(gt=0)


class ModelObservabilityPrometheus(ModelObservabilityContainer):
    retention_time: NonEmptyStr
    retention_size: NonEmptyStr
    scrape_interval_seconds: int = Field(gt=0)
    job_name: NonEmptyStr
    data_volume: NonEmptyStr


class ModelObservabilityGrafana(ModelObservabilityContainer):
    data_volume: NonEmptyStr
    dashboard_title: NonEmptyStr
    refresh_seconds: int = Field(gt=0)
    # A reading older than this window drops out of the oldest-reading panel; it bounds the lookback.
    reading_window_minutes: int = Field(gt=0)
    environment: dict[NonEmptyStr, str] = Field(default_factory=dict)


class ModelObservabilityAlerts(_Frozen):
    """The thresholds of the four alerts; each is a setting, none is a literal in the renderer."""

    host_down_minutes: int = Field(gt=0)
    disk_percent: int = Field(gt=0, le=100)
    inode_percent: int = Field(gt=0, le=100)
    load_minutes: int = Field(gt=0)
    failed_service_minutes: int = Field(gt=0)


class ModelObservabilityOverlay(_Frozen):
    ticket: NonEmptyStr
    observer_host: NonEmptyStr
    config_root: AbsPath
    docker_binary: AbsPath
    restart_policy: NonEmptyStr
    restart_seconds: int = Field(gt=0)
    stop_timeout_seconds: int = Field(gt=0)
    probe_wait_seconds: int = Field(gt=0)
    # The oldest reading AC1 allows, in seconds; the readback judges each host against it.
    max_reading_age_seconds: int = Field(gt=0)
    targets: dict[NonEmptyStr, ModelObservabilityTarget] = Field(min_length=1)
    exporter: ModelObservabilityExporter
    prometheus: ModelObservabilityPrometheus
    grafana: ModelObservabilityGrafana
    alerts: ModelObservabilityAlerts

    @model_validator(mode="after")
    def _observer_is_a_target(self) -> ModelObservabilityOverlay:
        if self.observer_host not in self.targets:
            raise ValueError(
                f"observer_host {self.observer_host} is not among the declared targets"
            )
        return self


class ModelObservabilityInstallRequest(_Frozen):
    """``host_name`` is the lab_hosts row this run installs on; ``overlay_file`` and ``hosts_file`` pin the sources."""

    host_name: NonEmptyStr
    mode: Mode
    overlay_file: str = ""
    hosts_file: str = ""


class ModelObservabilityInstallResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    uncovered_hosts: tuple[str, ...] = ()
