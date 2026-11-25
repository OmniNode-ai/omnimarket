# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document `node_host_observability_install_effect/overlay.yaml`: deployment values, none in code.

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
    # "install": the exporter container; "launchd": the exporter as a launchd job on a Mac host;
    # "existing": an exporter already answers there.
    exporter: Literal["install", "existing", "launchd"]
    port: int = Field(gt=0, lt=65536)
    # This host's own textfile directory; unset, the exporter section's ``textfile_directory`` applies.
    textfile_directory: AbsPath | None = None
    # True: this installer also keeps the per process exporter running on this host (CPU per process name).
    processes: bool = False


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
    # Further bind mounts (source:target:options), such as the host's system bus socket the systemd collector reads.
    host_mounts: tuple[NonEmptyStr, ...] = ()
    # Options for the container's security confinement (docker --security-opt), such as the profile the system bus accepts.
    security_opts: tuple[NonEmptyStr, ...] = ()
    # A regular expression: when set, the systemd collector reports only the units it matches.
    systemd_unit_include: str = ""
    network: NonEmptyStr
    memory_gib: int = Field(gt=0)
    memory_swap_gib: int = Field(gt=0)


class ModelObservabilityLaunchdExporter(_Frozen):
    """The exporter as a launchd job on a Mac host: a binary already on the host, run from a property list."""

    label: NonEmptyStr
    binary: AbsPath
    # The directory the property list is written to, and the one the job's logs go to.
    plist_directory: AbsPath
    log_directory: AbsPath
    textfile_directory: AbsPath
    # The launchctl domain the job is bootstrapped into (``system``, or ``gui/<uid>``).
    domain: NonEmptyStr
    enable_collectors: tuple[NonEmptyStr, ...] = ()


class ModelObservabilityProcessExporter(_Frozen):
    """The per process exporter container: host network, the host's process table read-only, one series per process name."""

    image: NonEmptyStr
    container: NonEmptyStr
    unit: NonEmptyStr
    network: NonEmptyStr
    port: int = Field(gt=0, lt=65536)
    job_name: NonEmptyStr
    # The host directory of the process table, mounted read-only into the container.
    procfs_mount: AbsPath
    # The exporter's own template for the name a process is grouped under (its command name by default).
    name_template: NonEmptyStr
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
    # A host whose newest reading is older than ``max_reading_age_seconds`` for this long is a silent machine.
    reading_stale_minutes: int = Field(gt=0)


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
    process_exporter: ModelObservabilityProcessExporter | None = None
    launchd_exporter: ModelObservabilityLaunchdExporter | None = None
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

    @model_validator(mode="after")
    def _processes_need_the_process_exporter(self) -> ModelObservabilityOverlay:
        asking = sorted(n for n, t in self.targets.items() if t.processes)
        if asking and self.process_exporter is None:
            raise ValueError(
                f"targets {asking} ask for the per process exporter but the settings declare no process_exporter section"
            )
        return self

    @model_validator(mode="after")
    def _launchd_targets_need_the_launchd_exporter(self) -> ModelObservabilityOverlay:
        asking = sorted(n for n, t in self.targets.items() if t.exporter == "launchd")
        if asking and self.launchd_exporter is None:
            raise ValueError(
                f"targets {asking} take the launchd exporter but the settings declare no launchd_exporter section"
            )
        return self
