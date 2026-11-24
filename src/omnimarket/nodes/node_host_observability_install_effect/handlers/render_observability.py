# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure rendering of the observability files and the container unit overlay from the settings.

Nothing here reads the host or the network: every value is an overlay field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityOverlay,
)

PROMETHEUS_DIR = "prometheus"
GRAFANA_DIR = "grafana"
PROVISIONING_DATASOURCE = f"{GRAFANA_DIR}/provisioning/datasources/prometheus.yaml"
PROVISIONING_DASHBOARDS = f"{GRAFANA_DIR}/provisioning/dashboards/lab-hosts.yaml"
DASHBOARD_FILE = f"{GRAFANA_DIR}/dashboards/lab-hosts.json"
GRAFANA_ENV_FILE = f"{GRAFANA_DIR}/grafana.env"
PROCESS_DIR = "process-exporter"
PROCESS_CONFIG = f"{PROCESS_DIR}/process-exporter.yml"
PROMETHEUS_CONFIG = f"{PROMETHEUS_DIR}/prometheus.yml"
PROMETHEUS_RULES = f"{PROMETHEUS_DIR}/rules.yml"
DATASOURCE_NAME = "lab-prometheus"
# CPU seconds per second of each process group, per host: the series the top-process panels and the readback rank.
PROCESS_CPU = (
    "sum by (host, groupname)(rate(namedprocess_namegroup_cpu_seconds_total[2m]))"
)


def _yaml(document: object) -> str:
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def render_prometheus_config(overlay: ModelObservabilityOverlay) -> str:
    interval = f"{overlay.prometheus.scrape_interval_seconds}s"
    jobs: list[dict[str, Any]] = [
        {
            "job_name": overlay.prometheus.job_name,
            "static_configs": [
                {
                    "targets": [f"{target.address}:{target.port}"],
                    "labels": {"host": name},
                }
                for name, target in sorted(overlay.targets.items())
            ],
        }
    ]
    process = overlay.process_exporter
    if process is not None:
        jobs.append(
            {
                "job_name": process.job_name,
                "static_configs": [
                    {
                        "targets": [f"{target.address}:{process.port}"],
                        "labels": {"host": name},
                    }
                    for name, target in sorted(overlay.targets.items())
                    if target.processes
                ],
            }
        )
    return _yaml(
        {
            "global": {"scrape_interval": interval, "evaluation_interval": interval},
            "rule_files": ["/etc/prometheus/rules.yml"],
            "scrape_configs": jobs,
        }
    )


def render_process_config(overlay: ModelObservabilityOverlay) -> str:
    """The per process exporter's grouping file: every process, under the name the template gives it."""
    process = overlay.process_exporter
    template = process.name_template if process is not None else ""
    return _yaml({"process_names": [{"name": template, "cmdline": [".+"]}]})


def render_rules(overlay: ModelObservabilityOverlay) -> str:
    a = overlay.alerts
    job = overlay.prometheus.job_name
    window = f"{overlay.grafana.reading_window_minutes}m"
    rules: list[dict[str, Any]] = [
        {
            "alert": "LabHostDown",
            "expr": f'up{{job="{job}"}} == 0',
            "for": f"{a.host_down_minutes}m",
            "annotations": {"summary": "{{ $labels.host }} exporter does not answer"},
        },
        {
            # A silent machine: its newest reading is older than the bound (or none lies in the window).
            "alert": "LabHostReadingStale",
            "expr": (
                f"time() - last_over_time(node_time_seconds[{window}]) "
                f"> {overlay.max_reading_age_seconds}"
            ),
            "for": f"{a.reading_stale_minutes}m",
            "annotations": {
                "summary": f"{{{{ $labels.host }}}} newest reading is older than {overlay.max_reading_age_seconds} seconds"
            },
        },
        {
            "alert": "LabHostDiskOver",
            "expr": (
                '100 * (1 - node_filesystem_avail_bytes{fstype!=""} '
                f'/ node_filesystem_size_bytes{{fstype!=""}}) > {a.disk_percent}'
            ),
            "annotations": {
                "summary": f"{{{{ $labels.host }}}} {{{{ $labels.mountpoint }}}} disk use over {a.disk_percent} percent"
            },
        },
        {
            "alert": "LabHostInodesOver",
            "expr": (
                '100 * (1 - node_filesystem_files_free{fstype!=""} '
                f'/ node_filesystem_files{{fstype!=""}}) > {a.inode_percent}'
            ),
            "annotations": {
                "summary": f"{{{{ $labels.host }}}} {{{{ $labels.mountpoint }}}} inode use over {a.inode_percent} percent"
            },
        },
        {
            "alert": "LabHostLoadAboveCores",
            "expr": (
                'node_load1 > on(host) count by (host)(node_cpu_seconds_total{mode="idle"})'
            ),
            "for": f"{a.load_minutes}m",
            "annotations": {
                "summary": "{{ $labels.host }} load is above its core count"
            },
        },
        {
            "alert": "LabHostServiceFailed",
            "expr": 'node_systemd_unit_state{state="failed"} == 1',
            "for": f"{a.failed_service_minutes}m",
            "annotations": {
                "summary": "{{ $labels.host }} service {{ $labels.name }} failed"
            },
        },
    ]
    process = overlay.process_exporter
    if process is not None:
        rules.append(
            {
                "alert": "LabProcessExporterDown",
                "expr": f'up{{job="{process.job_name}"}} == 0',
                "for": f"{a.host_down_minutes}m",
                "annotations": {
                    "summary": "{{ $labels.host }} per process exporter does not answer"
                },
            }
        )
    return _yaml({"groups": [{"name": "lab-hosts", "rules": rules}]})


def render_datasource(overlay: ModelObservabilityOverlay) -> str:
    return _yaml(
        {
            "apiVersion": 1,
            "datasources": [
                {
                    "name": DATASOURCE_NAME,
                    "uid": DATASOURCE_NAME,
                    "type": "prometheus",
                    "access": "proxy",
                    "url": f"http://{overlay.prometheus.address}:{overlay.prometheus.port}",
                    "isDefault": True,
                }
            ],
        }
    )


def render_dashboard_provider() -> str:
    return _yaml(
        {
            "apiVersion": 1,
            "providers": [
                {
                    "name": "lab-hosts",
                    "type": "file",
                    "disableDeletion": True,
                    "options": {"path": "/var/lib/grafana-dashboards"},
                }
            ],
        }
    )


def _panel(
    panel_id: int,
    title: str,
    expr: str,
    kind: str,
    x: int,
    y: int,
    w: int,
    h: int,
    unit: str = "",
    legend: str = "{{host}}",
    no_value: str = "",
) -> dict[str, Any]:
    panel: dict[str, Any] = {
        "id": panel_id,
        "title": title,
        "type": kind,
        "datasource": {"type": "prometheus", "uid": DATASOURCE_NAME},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {
                "expr": expr,
                "legendFormat": legend,
                "refId": "A",
                "instant": kind in ("stat", "table"),
            }
        ],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    }
    if no_value:
        panel["fieldConfig"]["defaults"]["noValue"] = no_value
    return panel


def render_dashboard(overlay: ModelObservabilityOverlay) -> str:
    job = overlay.prometheus.job_name
    window = f"{overlay.grafana.reading_window_minutes}m"
    panels = [
        _panel(
            1,
            "Read at",
            # time() is seconds; the dateTimeAsIso unit reads milliseconds (seconds showed as 1970).
            "time() * 1000",
            "stat",
            0,
            0,
            4,
            4,
            unit="dateTimeAsIso",
            legend="evaluated",
        ),
        _panel(
            2,
            "Hosts up (1 up, 0 down)",
            f'up{{job="{job}"}}',
            "stat",
            4,
            0,
            8,
            4,
        ),
        _panel(
            3,
            "Oldest reading, seconds old",
            f"max(time() - last_over_time(node_time_seconds[{window}]))",
            "stat",
            12,
            0,
            6,
            4,
            unit="s",
            legend="oldest",
        ),
        _panel(
            4,
            "Failed services",
            'node_systemd_unit_state{state="failed"} == 1',
            "table",
            18,
            0,
            6,
            4,
            legend="{{host}} {{name}}",
            no_value="none failed",
        ),
        _panel(
            5,
            "Reading age per host, seconds",
            f"time() - last_over_time(node_time_seconds[{window}])",
            "stat",
            0,
            4,
            12,
            4,
            unit="s",
        ),
        _panel(
            12,
            "Failed services per host",
            'sum by (host)(node_systemd_unit_state{state="failed"})',
            "stat",
            12,
            4,
            12,
            4,
            no_value="no systemd reading",
        ),
        _panel(
            6,
            "CPU use",
            '100 - avg by (host)(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100',
            "timeseries",
            0,
            8,
            12,
            7,
            unit="percent",
        ),
        _panel(
            7,
            "Load 1m over cores",
            'node_load1 / on(host) count by (host)(node_cpu_seconds_total{mode="idle"})',
            "timeseries",
            12,
            8,
            12,
            7,
        ),
        _panel(
            8,
            "Memory use",
            "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
            "timeseries",
            0,
            15,
            8,
            7,
            unit="percent",
        ),
        _panel(
            9,
            "Swap use",
            "100 * (1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)",
            "timeseries",
            8,
            15,
            8,
            7,
            unit="percent",
        ),
        _panel(
            10,
            "Disk use",
            '100 * (1 - node_filesystem_avail_bytes{fstype!=""} / node_filesystem_size_bytes{fstype!=""})',
            "timeseries",
            16,
            15,
            8,
            7,
            unit="percent",
            legend="{{host}} {{mountpoint}}",
        ),
        _panel(
            11,
            "Inode use",
            '100 * (1 - node_filesystem_files_free{fstype!=""} / node_filesystem_files{fstype!=""})',
            "timeseries",
            0,
            22,
            12,
            7,
            unit="percent",
            legend="{{host}} {{mountpoint}}",
        ),
    ]
    process = overlay.process_exporter
    if process is not None:
        cpu = PROCESS_CPU
        panels += [
            _panel(
                13,
                "Top processes by CPU",
                f"topk(10, {cpu})",
                "table",
                0,
                29,
                12,
                8,
                legend="{{host}} {{groupname}}",
                no_value="no process reading",
            ),
            _panel(
                14,
                "Process CPU over time",
                f"topk(5, {cpu})",
                "timeseries",
                12,
                29,
                12,
                8,
                legend="{{host}} {{groupname}}",
            ),
        ]
    return json.dumps(
        {
            "uid": "lab-hosts",
            "title": overlay.grafana.dashboard_title,
            "schemaVersion": 39,
            "version": 1,
            "refresh": f"{overlay.grafana.refresh_seconds}s",
            "time": {"from": "now-3h", "to": "now"},
            "timepicker": {},
            "panels": panels,
        },
        indent=2,
        sort_keys=True,
    )


def render_grafana_env(overlay: ModelObservabilityOverlay) -> str:
    env = {
        "GF_SERVER_HTTP_PORT": str(overlay.grafana.port),
        **overlay.grafana.environment,
    }
    return "".join(f"{key}={value}\n" for key, value in sorted(env.items()))


def render_files(overlay: ModelObservabilityOverlay) -> dict[str, str]:
    """Relative path under ``config_root`` -> file text, for the observer host."""
    files = {
        PROMETHEUS_CONFIG: render_prometheus_config(overlay),
        PROMETHEUS_RULES: render_rules(overlay),
        PROVISIONING_DATASOURCE: render_datasource(overlay),
        PROVISIONING_DASHBOARDS: render_dashboard_provider(),
        DASHBOARD_FILE: render_dashboard(overlay),
        GRAFANA_ENV_FILE: render_grafana_env(overlay),
    }
    if overlay.process_exporter is not None:
        files[PROCESS_CONFIG] = render_process_config(overlay)
    return files


def render_host_files(overlay: ModelObservabilityOverlay, host: str) -> dict[str, str]:
    """The files this host takes: the observer's whole set, or only the process grouping file."""
    if host == overlay.observer_host:
        return render_files(overlay)
    target = overlay.targets.get(host)
    if target is not None and target.processes and overlay.process_exporter is not None:
        return {PROCESS_CONFIG: render_process_config(overlay)}
    return {}


def _unit_base(
    overlay: ModelObservabilityOverlay, host: str, name: str, container: str
) -> dict[str, Any]:
    return {
        "name": name,
        "hosts": [host],
        "container": container,
        "ticket": overlay.ticket,
        "docker_binary": overlay.docker_binary,
        "restart_seconds": overlay.restart_seconds,
        "stop_timeout_seconds": overlay.stop_timeout_seconds,
        "probe_wait_seconds": overlay.probe_wait_seconds,
    }


def units_for_host(overlay: ModelObservabilityOverlay, host: str) -> list[str]:
    """The unit names this host takes, in install order (exporter, Prometheus, Grafana)."""
    target = overlay.targets.get(host)
    names: list[str] = []
    if target is not None and target.exporter == "install":
        names.append(overlay.exporter.unit)
    if target is not None and target.processes and overlay.process_exporter is not None:
        names.append(overlay.process_exporter.unit)
    if host == overlay.observer_host:
        names += [overlay.prometheus.unit, overlay.grafana.unit]
    return names


def render_container_units_overlay(
    overlay: ModelObservabilityOverlay, host: str
) -> dict[str, Any]:
    """The overlay document the container unit installer reads for this host's units."""
    root = overlay.config_root
    units: list[dict[str, Any]] = []
    target = overlay.targets.get(host)
    ex = overlay.exporter
    if target is not None and target.exporter == "install":
        units.append(
            {
                **_unit_base(overlay, host, ex.unit, ex.container),
                "probe_address": target.address,
                "probe_port": target.port,
                "network": ex.network,
                "create": {
                    "image": ex.image,
                    "command": [
                        "--path.rootfs=/host",
                        f"--web.listen-address={target.address}:{target.port}",
                        "--collector.textfile.directory=/textfile",
                        *[f"--collector.{name}" for name in ex.enable_collectors],
                        *(
                            [
                                f"--collector.systemd.unit-include={ex.systemd_unit_include}"
                            ]
                            if ex.systemd_unit_include
                            else []
                        ),
                    ],
                    "volumes": [
                        f"{ex.rootfs_mount}:/host:ro,rslave",
                        f"{ex.textfile_directory}:/textfile:ro",
                        *ex.host_mounts,
                    ],
                    "restart_policy": overlay.restart_policy,
                    "security_opts": list(ex.security_opts),
                },
                "memory_gib": ex.memory_gib,
                "memory_swap_gib": ex.memory_swap_gib,
            }
        )
    proc = overlay.process_exporter
    if target is not None and target.processes and proc is not None:
        units.append(
            {
                **_unit_base(overlay, host, proc.unit, proc.container),
                "probe_address": target.address,
                "probe_port": proc.port,
                "network": proc.network,
                "create": {
                    "image": proc.image,
                    "command": [
                        f"-config.path=/config/{Path(PROCESS_CONFIG).name}",
                        "-procfs=/host/proc",
                        f"-web.listen-address={target.address}:{proc.port}",
                    ],
                    "volumes": [
                        f"{proc.procfs_mount}:/host/proc:ro",
                        f"{root}/{PROCESS_DIR}:/config:ro",
                    ],
                    "restart_policy": overlay.restart_policy,
                },
                "memory_gib": proc.memory_gib,
                "memory_swap_gib": proc.memory_swap_gib,
            }
        )
    if host == overlay.observer_host:
        prom, graf = overlay.prometheus, overlay.grafana
        units.append(
            {
                **_unit_base(overlay, host, prom.unit, prom.container),
                "probe_address": prom.address,
                "probe_port": prom.port,
                "network": prom.network,
                "published_ports": [
                    {
                        "host_address": prom.address,
                        "host_port": prom.port,
                        "container_port": prom.port,
                    }
                ],
                "create": {
                    "image": prom.image,
                    "command": [
                        "--config.file=/etc/prometheus/prometheus.yml",
                        "--storage.tsdb.path=/prometheus",
                        f"--storage.tsdb.retention.time={prom.retention_time}",
                        f"--storage.tsdb.retention.size={prom.retention_size}",
                        f"--web.listen-address=:{prom.port}",
                    ],
                    "volumes": [
                        f"{root}/{PROMETHEUS_DIR}:/etc/prometheus:ro",
                        f"{prom.data_volume}:/prometheus",
                    ],
                    "restart_policy": overlay.restart_policy,
                },
                "memory_gib": prom.memory_gib,
                "memory_swap_gib": prom.memory_swap_gib,
            }
        )
        units.append(
            {
                **_unit_base(overlay, host, graf.unit, graf.container),
                "probe_address": graf.address,
                "probe_port": graf.port,
                "network": graf.network,
                "published_ports": [
                    {
                        "host_address": graf.address,
                        "host_port": graf.port,
                        "container_port": graf.port,
                    }
                ],
                "create": {
                    "image": graf.image,
                    "volumes": [
                        f"{root}/{GRAFANA_DIR}/provisioning:/etc/grafana/provisioning:ro",
                        f"{root}/{GRAFANA_DIR}/dashboards:/var/lib/grafana-dashboards:ro",
                        f"{graf.data_volume}:/var/lib/grafana",
                    ],
                    "restart_policy": overlay.restart_policy,
                    "env_file": f"{root}/{GRAFANA_ENV_FILE}",
                },
                "memory_gib": graf.memory_gib,
                "memory_swap_gib": graf.memory_swap_gib,
            }
        )
    return {"units": units}
