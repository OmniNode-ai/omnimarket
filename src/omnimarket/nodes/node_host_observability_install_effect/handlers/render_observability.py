# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure rendering of the observability files and the container unit overlay from the settings.

Nothing here reads the host or the network: every value is an overlay field.
"""

from __future__ import annotations

import json
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
PROMETHEUS_CONFIG = f"{PROMETHEUS_DIR}/prometheus.yml"
PROMETHEUS_RULES = f"{PROMETHEUS_DIR}/rules.yml"
DATASOURCE_NAME = "lab-prometheus"


def _yaml(document: object) -> str:
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def render_prometheus_config(overlay: ModelObservabilityOverlay) -> str:
    interval = f"{overlay.prometheus.scrape_interval_seconds}s"
    return _yaml(
        {
            "global": {"scrape_interval": interval, "evaluation_interval": interval},
            "rule_files": ["/etc/prometheus/rules.yml"],
            "scrape_configs": [
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
            ],
        }
    )


def render_rules(overlay: ModelObservabilityOverlay) -> str:
    a = overlay.alerts
    job = overlay.prometheus.job_name
    rules: list[dict[str, Any]] = [
        {
            "alert": "LabHostDown",
            "expr": f'up{{job="{job}"}} == 0',
            "for": f"{a.host_down_minutes}m",
            "annotations": {"summary": "{{ $labels.host }} exporter does not answer"},
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
    return panel


def render_dashboard(overlay: ModelObservabilityOverlay) -> str:
    job = overlay.prometheus.job_name
    window = f"{overlay.grafana.reading_window_minutes}m"
    panels = [
        _panel(
            1,
            "Read at",
            "time()",
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
        ),
        _panel(
            5,
            "Reading age per host, seconds",
            f"time() - last_over_time(node_time_seconds[{window}])",
            "stat",
            0,
            4,
            24,
            4,
            unit="s",
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
    return {
        PROMETHEUS_CONFIG: render_prometheus_config(overlay),
        PROMETHEUS_RULES: render_rules(overlay),
        PROVISIONING_DATASOURCE: render_datasource(overlay),
        PROVISIONING_DASHBOARDS: render_dashboard_provider(),
        DASHBOARD_FILE: render_dashboard(overlay),
        GRAFANA_ENV_FILE: render_grafana_env(overlay),
    }


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
                    ],
                    "volumes": [
                        f"{ex.rootfs_mount}:/host:ro,rslave",
                        f"{ex.textfile_directory}:/textfile:ro",
                    ],
                    "restart_policy": overlay.restart_policy,
                },
                "memory_gib": ex.memory_gib,
                "memory_swap_gib": ex.memory_swap_gib,
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
