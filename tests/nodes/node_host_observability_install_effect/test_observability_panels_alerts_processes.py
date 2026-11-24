# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observability install (OMN-26657): the panel defects, the per process series and the silent-machine alerts.

Each test pins one defect read back from the live page: the Read at panel shown at 1970 (seconds given to a
milliseconds unit), the Failed services panel with No data (the systemd collector could not reach the bus and an
empty ``== 1`` filter shows nothing), no per process CPU series, and no alert for a machine whose reading is old.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerUnitsOverlay,
)
from omnimarket.nodes.node_host_observability_install_effect.handlers.handler_observability_install import (
    HandlerObservabilityInstall,
)
from omnimarket.nodes.node_host_observability_install_effect.handlers.render_observability import (
    DASHBOARD_FILE,
    PROCESS_CONFIG,
    PROMETHEUS_CONFIG,
    PROMETHEUS_RULES,
    render_container_units_overlay,
    render_files,
    units_for_host,
)
from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityInstallRequest,
    ModelObservabilityOverlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_host_observability_install_effect/overlay.example.yaml"
)


def _document(tmp_path: Path) -> dict[str, Any]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["config_root"] = str(tmp_path / "config")
    document["exporter"]["textfile_directory"] = str(tmp_path / "textfile")
    return document


def _overlay(document: dict[str, Any]) -> ModelObservabilityOverlay:
    return ModelObservabilityOverlay.model_validate(document)


def _panels(overlay: ModelObservabilityOverlay) -> dict[str, dict[str, Any]]:
    dashboard = json.loads(render_files(overlay)[DASHBOARD_FILE])
    return {p["title"]: p for p in dashboard["panels"]}


def _rules(overlay: ModelObservabilityOverlay) -> dict[str, dict[str, Any]]:
    rules = yaml.safe_load(render_files(overlay)[PROMETHEUS_RULES])
    return {r["alert"]: r for r in rules["groups"][0]["rules"]}


# --- Defect 1: Read at at 1970 -------------------------------------------------------------------------------------


def test_read_at_panel_gives_milliseconds_to_the_iso_time_unit(tmp_path: Path) -> None:
    panel = _panels(_overlay(_document(tmp_path)))["Read at"]
    assert panel["fieldConfig"]["defaults"]["unit"] == "dateTimeAsIso"
    # Prometheus time() is seconds; Grafana's dateTimeAsIso reads milliseconds, so seconds read as 1970.
    assert panel["targets"][0]["expr"] == "time() * 1000"


# --- Defect 2: Failed services with No data ---------------------------------------------------------------------------


def test_failed_services_shows_a_value_when_none_failed_and_counts_per_host(
    tmp_path: Path,
) -> None:
    panels = _panels(_overlay(_document(tmp_path)))
    table = panels["Failed services"]
    assert table["fieldConfig"]["defaults"]["noValue"] == "none failed"
    count = panels["Failed services per host"]
    assert count["targets"][0]["expr"] == (
        'sum by (host)(node_systemd_unit_state{state="failed"})'
    )


def test_exporter_reaches_the_systemd_bus_through_a_declared_mount(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    overlay = _overlay(document)
    units = render_container_units_overlay(overlay, "host-a")["units"]
    exporter = next(u for u in units if u["name"] == "example-node-exporter")
    mount = document["exporter"]["host_mounts"][0]
    assert mount in exporter["create"]["volumes"]
    assert (
        f"--collector.systemd.unit-include={document['exporter']['systemd_unit_include']}"
        in exporter["create"]["command"]
    )
    assert "--collector.systemd" in exporter["create"]["command"]
    # The host's system bus refuses the image's default confinement profile, so the declared options are passed on.
    assert exporter["create"]["security_opts"] == document["exporter"]["security_opts"]
    # Positive control: with no mount declared none is rendered.
    document["exporter"]["host_mounts"] = []
    bare = render_container_units_overlay(_overlay(document), "host-a")["units"][0]
    assert mount not in bare["create"]["volumes"]


# --- Defect 3: no per process series ----------------------------------------------------------------------------------


def test_process_exporter_unit_is_a_valid_container_unit_on_every_host_that_takes_it(
    tmp_path: Path,
) -> None:
    overlay = _overlay(_document(tmp_path))
    # host-b has an existing node exporter yet takes the process exporter.
    assert units_for_host(overlay, "host-b") == ["example-process-exporter"]
    assert units_for_host(overlay, "host-a") == [
        "example-node-exporter",
        "example-process-exporter",
        "example-prometheus",
        "example-grafana",
    ]
    for host in ("host-a", "host-b"):
        parsed = ModelContainerUnitsOverlay.model_validate(
            render_container_units_overlay(overlay, host)
        )
        process = next(u for u in parsed.units if u.name == "example-process-exporter")
        command = process.create.command  # type: ignore[union-attr]
        assert "-procfs=/host/proc" in command
        assert (
            f"-web.listen-address={process.probe_address}:{process.probe_port}"
            in command
        )
        assert any(c.startswith("-config.path=") for c in command)
        volumes = process.create.volumes  # type: ignore[union-attr]
        assert "/proc:/host/proc:ro" in volumes
        assert process.probe_port == 9256


def test_a_target_without_the_processes_flag_takes_no_process_exporter(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    document["targets"]["host-b"]["processes"] = False
    overlay = _overlay(document)
    assert units_for_host(overlay, "host-b") == []
    config = yaml.safe_load(render_files(overlay)[PROMETHEUS_CONFIG])
    jobs = {j["job_name"]: j for j in config["scrape_configs"]}
    assert jobs["lab_processes"]["static_configs"] == [
        {"targets": ["host-a.example.test:9256"], "labels": {"host": "host-a"}}
    ]


def test_a_process_target_without_process_exporter_settings_is_refused(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    del document["process_exporter"]
    with pytest.raises(ValueError, match="process_exporter"):
        _overlay(document)
    # Positive control: with no target asking for processes the section is optional.
    for target in document["targets"].values():
        target["processes"] = False
    assert _overlay(document).process_exporter is None


def test_prometheus_scrapes_the_process_exporter_with_the_same_host_label(
    tmp_path: Path,
) -> None:
    config = yaml.safe_load(
        render_files(_overlay(_document(tmp_path)))[PROMETHEUS_CONFIG]
    )
    jobs = {j["job_name"]: j for j in config["scrape_configs"]}
    assert set(jobs) == {"lab_hosts", "lab_processes"}
    assert jobs["lab_processes"]["static_configs"] == [
        {"targets": ["host-a.example.test:9256"], "labels": {"host": "host-a"}},
        {"targets": ["host-b.example.test:9256"], "labels": {"host": "host-b"}},
    ]


def test_process_exporter_config_names_every_process_from_the_template_setting(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    config = yaml.safe_load(render_files(_overlay(document))[PROCESS_CONFIG])
    assert config["process_names"][0]["name"] == "{{.Comm}}"
    document["process_exporter"]["name_template"] = "{{.ExeBase}}"
    changed = yaml.safe_load(render_files(_overlay(document))[PROCESS_CONFIG])
    assert changed["process_names"][0]["name"] == "{{.ExeBase}}"


def test_dashboard_names_the_top_process_by_cpu(tmp_path: Path) -> None:
    panels = _panels(_overlay(_document(tmp_path)))
    top = panels["Top processes by CPU"]
    expr = top["targets"][0]["expr"]
    assert "namedprocess_namegroup_cpu_seconds_total" in expr
    assert expr.startswith("topk(")
    assert top["targets"][0]["legendFormat"] == "{{host}} {{groupname}}"
    series = panels["Process CPU over time"]
    assert "namedprocess_namegroup_cpu_seconds_total" in series["targets"][0]["expr"]


def test_install_writes_the_process_config_on_a_non_observer_host(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(command: Sequence[str]):  # type: ignore[no-untyped-def]
        import subprocess

        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "ok\n", "")

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_document(tmp_path)), encoding="utf-8")
    handler = HandlerObservabilityInstall(
        run=run,
        hosts=lambda _f: ModelLabHostsTable.model_validate(
            {"hosts": [{"name": n, "target": n} for n in ("host-a", "host-b")]}
        ),
        python="/usr/bin/python-test",
        environ={},
    )
    result = handler.handle(
        ModelObservabilityInstallRequest(
            host_name="host-b", mode="install", overlay_file=str(overlay_file)
        )
    )
    assert result.exit_code == 0
    assert (tmp_path / "config" / PROCESS_CONFIG).is_file()
    assert [c[c.index("--unit") + 1] for c in calls if "--unit" in c] == [
        "example-process-exporter"
    ]


# --- Defect 4: no alert for a silent machine ------------------------------------------------------------------------


def test_a_stale_reading_and_a_down_target_each_have_an_alert_from_settings(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    rules = _rules(_overlay(document))
    stale = rules["LabHostReadingStale"]
    assert "last_over_time(node_time_seconds[30m])" in stale["expr"]
    assert stale["expr"].endswith("> 60")
    assert stale["for"] == "2m"
    assert rules["LabHostDown"]["expr"] == 'up{job="lab_hosts"} == 0'
    assert rules["LabProcessExporterDown"]["expr"] == 'up{job="lab_processes"} == 0'
    # Positive control: the bound and the hold time are settings.
    document["max_reading_age_seconds"] = 45
    document["alerts"]["reading_stale_minutes"] = 5
    changed = _rules(_overlay(document))["LabHostReadingStale"]
    assert changed["expr"].endswith("> 45")
    assert changed["for"] == "5m"


def test_no_process_alert_is_rendered_when_no_process_exporter_is_declared(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    for target in document["targets"].values():
        target["processes"] = False
    del document["process_exporter"]
    assert "LabProcessExporterDown" not in _rules(_overlay(document))


# --- Readback ----------------------------------------------------------------------------------------------------------


def test_readback_names_the_top_process_of_each_host(tmp_path: Path) -> None:
    def get(url: str) -> str:
        if "namedprocess" in url:
            rows = [
                {
                    "metric": {"host": "host-a", "groupname": "python3"},
                    "value": [0, "3.5"],
                },
                {
                    "metric": {"host": "host-b", "groupname": "dockerd"},
                    "value": [0, "0.4"],
                },
            ]
        elif "last_over_time" in url:
            rows = [
                {"metric": {"host": "host-a"}, "value": [0, "3"]},
                {"metric": {"host": "host-b"}, "value": [0, "4"]},
            ]
        else:
            rows = [
                {"metric": {"host": "host-a"}, "value": [0, "1"]},
                {"metric": {"host": "host-b"}, "value": [0, "1"]},
            ]
        return json.dumps({"status": "success", "data": {"result": rows}})

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_document(tmp_path)), encoding="utf-8")
    handler = HandlerObservabilityInstall(
        hosts=lambda _f: ModelLabHostsTable.model_validate(
            {"hosts": [{"name": n, "target": n} for n in ("host-a", "host-b")]}
        ),
        get=get,
        environ={},
    )
    result = handler.handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="readback", overlay_file=str(overlay_file)
        )
    )
    assert result.exit_code == 0
    assert "TOP PROCESS host-a python3 cpu=3.50" in result.stdout
    assert "TOP PROCESS host-b dockerd cpu=0.40" in result.stdout


# --- The collaborator's machines are never scraped, never counted ---------------------------------------------------


def _table_with_collaborator() -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {"name": "host-a", "target": "host-a"},
                {"name": "host-b", "target": "host-b"},
                {
                    "name": "host-x",
                    "target": "host-x",
                    "contributor": True,
                    "lane_binding": "example-lane",
                    "lane_not_ready_reason": "owned by its owner",
                },
            ]
        }
    )


def test_readback_leaves_a_collaborator_machine_out_of_the_count(
    tmp_path: Path,
) -> None:
    def get(url: str) -> str:
        rows = [
            {
                "metric": {"host": h},
                "value": [0, "1" if "last_over" not in url else "2"],
            }
            for h in ("host-a", "host-b")
        ]
        return json.dumps({"status": "success", "data": {"result": rows}})

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(_document(tmp_path)), encoding="utf-8")
    result = HandlerObservabilityInstall(
        hosts=lambda _f: _table_with_collaborator(), get=get, environ={}
    ).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="readback", overlay_file=str(overlay_file)
        )
    )
    assert result.exit_code == 0
    assert "COVERED 2 of 2" in result.stdout
    assert "NO TARGET host-x" not in result.stdout
    assert "EXCLUDED host-x" in result.stdout
    assert result.uncovered_hosts == ()


def test_a_target_naming_a_collaborator_machine_is_refused(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["targets"]["host-x"] = {
        "address": "host-x.example.test",
        "exporter": "existing",
        "port": 9100,
    }
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = HandlerObservabilityInstall(
        hosts=lambda _f: _table_with_collaborator(), environ={}
    ).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="install", overlay_file=str(overlay_file)
        )
    )
    assert result.exit_code == 1
    assert "host-x" in result.stderr
    assert "collaborator" in result.stderr
