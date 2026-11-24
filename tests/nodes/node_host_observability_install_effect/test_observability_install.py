# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observability install (OMN-26657): rendering from settings, the install through the container unit installer, the readback."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerUnitsOverlay,
)
from omnimarket.nodes.node_host_observability_install_effect.handlers.handler_observability_install import (
    CONTAINER_UNITS_MODULE,
    HandlerObservabilityInstall,
)
from omnimarket.nodes.node_host_observability_install_effect.handlers.render_observability import (
    DASHBOARD_FILE,
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
from omnimarket.nodes.node_host_observability_install_effect.node import (
    NodeHostObservabilityInstallEffect,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_host_observability_install_effect/overlay.example.yaml"
)


def _document(tmp_path: Path) -> dict[str, object]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["config_root"] = str(tmp_path / "config")
    document["exporter"]["textfile_directory"] = str(tmp_path / "textfile")
    return document


def _overlay(tmp_path: Path) -> ModelObservabilityOverlay:
    return ModelObservabilityOverlay.model_validate(_document(tmp_path))


def _table(names: Sequence[str]) -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(
        {"hosts": [{"name": n, "target": n} for n in names]}
    )


def _write_overlay(tmp_path: Path, document: dict[str, object]) -> str:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


class Recorder:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), self.code, "ok\n", "")


def _handler(
    names: Sequence[str], run: Recorder | None = None, get: object = None
) -> HandlerObservabilityInstall:
    kwargs: dict[str, object] = {
        "run": run or Recorder(),
        "hosts": lambda _file: _table(names),
        "python": "/usr/bin/python-test",
        "environ": {},
    }
    if get is not None:
        kwargs["get"] = get
    return HandlerObservabilityInstall(**kwargs)  # type: ignore[arg-type]


def test_rules_carry_the_declared_thresholds_and_a_changed_setting_changes_them(
    tmp_path: Path,
) -> None:
    rules = yaml.safe_load(render_files(_overlay(tmp_path))[PROMETHEUS_RULES])
    by_name = {r["alert"]: r for r in rules["groups"][0]["rules"]}
    assert set(by_name) == {
        "LabHostDown",
        "LabHostDiskOver",
        "LabHostInodesOver",
        "LabHostLoadAboveCores",
        "LabHostServiceFailed",
    }
    assert "> 85" in by_name["LabHostDiskOver"]["expr"]
    assert "> 85" in by_name["LabHostInodesOver"]["expr"]
    assert by_name["LabHostLoadAboveCores"]["for"] == "10m"
    # Positive control: the threshold is a setting, not a literal.
    document = _document(tmp_path)
    document["alerts"]["disk_percent"] = 70  # type: ignore[index]
    document["alerts"]["load_minutes"] = 3  # type: ignore[index]
    changed = ModelObservabilityOverlay.model_validate(document)
    rules2 = yaml.safe_load(render_files(changed)[PROMETHEUS_RULES])
    by2 = {r["alert"]: r for r in rules2["groups"][0]["rules"]}
    assert "> 70" in by2["LabHostDiskOver"]["expr"]
    assert by2["LabHostLoadAboveCores"]["for"] == "3m"


def test_prometheus_config_scrapes_each_declared_target_with_its_host_label(
    tmp_path: Path,
) -> None:
    config = yaml.safe_load(render_files(_overlay(tmp_path))[PROMETHEUS_CONFIG])
    job = config["scrape_configs"][0]
    assert job["job_name"] == "lab_hosts"
    assert config["global"]["scrape_interval"] == "15s"
    assert job["static_configs"] == [
        {"targets": ["host-a.example.test:9100"], "labels": {"host": "host-a"}},
        {"targets": ["host-b.example.test:9100"], "labels": {"host": "host-b"}},
    ]


def test_dashboard_shows_read_time_oldest_reading_and_failed_services(
    tmp_path: Path,
) -> None:
    dashboard = json.loads(render_files(_overlay(tmp_path))[DASHBOARD_FILE])
    titles = {p["title"]: p for p in dashboard["panels"]}
    assert titles["Read at"]["targets"][0]["expr"] == "time()"
    assert (
        "last_over_time(node_time_seconds[30m])"
        in titles["Oldest reading, seconds old"]["targets"][0]["expr"]
    )
    assert "failed" in titles["Failed services"]["targets"][0]["expr"]
    assert dashboard["refresh"] == "15s"


def test_container_unit_overlay_is_accepted_by_the_container_unit_installer_model(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    exporter_host = render_container_units_overlay(overlay, "host-a")
    parsed = ModelContainerUnitsOverlay.model_validate(exporter_host)
    assert [u.name for u in parsed.units] == [
        "example-node-exporter",
        "example-prometheus",
        "example-grafana",
    ]
    prom = parsed.units[1]
    assert prom.published_ports[0].host_address == "host-a.example.test"
    assert any(
        c == "--storage.tsdb.retention.size=20GB"
        for c in prom.create.command  # type: ignore[union-attr]
    )
    # host-b has an existing exporter: the installer puts no unit there.
    assert units_for_host(overlay, "host-b") == []
    assert render_container_units_overlay(overlay, "host-b") == {"units": []}


def test_install_writes_files_then_runs_the_container_unit_installer_per_unit(
    tmp_path: Path,
) -> None:
    run = Recorder()
    overlay_file = _write_overlay(tmp_path, _document(tmp_path))
    result = _handler(["host-a", "host-b", "host-c"], run).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="install", overlay_file=overlay_file
        )
    )
    assert result.exit_code == 0
    assert result.uncovered_hosts == ("host-c",)
    config = tmp_path / "config"
    assert (config / PROMETHEUS_CONFIG).is_file()
    assert (config / DASHBOARD_FILE).is_file()
    assert (tmp_path / "textfile").is_dir()
    installs = [c for c in run.calls if CONTAINER_UNITS_MODULE in c]
    assert [c[c.index("--unit") + 1] for c in installs] == [
        "example-node-exporter",
        "example-prometheus",
        "example-grafana",
    ]
    assert all(c[0] == "/usr/bin/python-test" and c[3] == "install" for c in installs)
    assert installs[0][installs[0].index("--overlay") + 1] == str(
        config / "container-units.overlay.yaml"
    )
    # The first install changed the Prometheus files, so Prometheus is told to reload.
    assert run.calls[-1] == [
        "/usr/bin/docker",
        "kill",
        "--signal=HUP",
        "example-prometheus",
    ]
    # A second install changes no file and so reloads nothing.
    again = Recorder()
    _handler(["host-a", "host-b"], again).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="install", overlay_file=overlay_file
        )
    )
    assert not any("kill" in c for c in again.calls)


def test_a_failed_unit_install_stops_the_run_and_reports_its_exit(
    tmp_path: Path,
) -> None:
    run = Recorder(code=1)
    result = _handler(["host-a", "host-b"], run).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="install",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 1
    assert len([c for c in run.calls if CONTAINER_UNITS_MODULE in c]) == 1


def test_a_target_that_is_not_a_lab_hosts_row_is_refused_with_nothing_done(
    tmp_path: Path,
) -> None:
    run = Recorder()
    result = _handler(["host-a"], run).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="install",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 1
    assert "host-b" in result.stderr
    assert run.calls == []
    assert not (tmp_path / "config").exists()


def test_a_host_with_no_unit_is_refused(tmp_path: Path) -> None:
    result = _handler(["host-a", "host-b"]).handle(
        ModelObservabilityInstallRequest(
            host_name="host-b",
            mode="install",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 1
    assert "takes no unit" in result.stderr


def test_dry_run_prints_files_and_writes_nothing(tmp_path: Path) -> None:
    result = _handler(["host-a", "host-b"]).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="dry_run",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 0
    assert "prometheus.yml" in result.stdout
    assert not (tmp_path / "config").exists()


def _prometheus_answer(up: dict[str, float], age: dict[str, float]):  # type: ignore[no-untyped-def]
    def get(url: str) -> str:
        data = age if "last_over_time" in url else up
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"host": h}, "value": [0, str(v)]}
                        for h, v in data.items()
                    ]
                },
            }
        )

    return get


def test_readback_names_fresh_stale_down_and_untargeted_hosts(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["targets"]["host-c"] = {  # type: ignore[index]
        "address": "host-c.example.test",
        "exporter": "existing",
        "port": 9100,
    }
    overlay_file = _write_overlay(tmp_path, document)
    get = _prometheus_answer(
        {"host-a": 1.0, "host-b": 1.0, "host-c": 0.0}, {"host-a": 4.0, "host-b": 300.0}
    )
    result = _handler(["host-a", "host-b", "host-c", "host-d"], get=get).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="readback", overlay_file=overlay_file
        )
    )
    assert result.exit_code == 1
    assert "READ host-a up=1 age=4s" in result.stdout
    assert "STALE host-b age=300s" in result.stdout
    assert "DOWN host-c" in result.stdout
    assert "NO TARGET host-d" in result.stdout
    assert "COVERED 1 of 4" in result.stdout


def test_readback_exits_zero_only_when_every_lab_hosts_row_reads_fresh(
    tmp_path: Path,
) -> None:
    get = _prometheus_answer(
        {"host-a": 1.0, "host-b": 1.0}, {"host-a": 2.0, "host-b": 58.0}
    )
    result = _handler(["host-a", "host-b"], get=get).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="readback",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 0
    assert "COVERED 2 of 2" in result.stdout


def test_an_unreadable_prometheus_is_indeterminate_not_zero(tmp_path: Path) -> None:
    def refuse(url: str) -> str:
        raise OSError("refused")

    result = _handler(["host-a", "host-b"], get=refuse).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="readback",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 3


def test_node_entry_delegates_to_the_handler(tmp_path: Path) -> None:
    node = NodeHostObservabilityInstallEffect()
    node.handler = _handler(["host-a", "host-b"])
    result = node.handle(
        ModelObservabilityInstallRequest(
            host_name="host-a",
            mode="dry_run",
            overlay_file=_write_overlay(tmp_path, _document(tmp_path)),
        )
    )
    assert result.exit_code == 0
    assert node.contract_path.is_file()


def test_node_source_holds_no_address_or_host_path_literal() -> None:
    """AC5: no address, port or host path literal in code; those are overlay settings.

    The mount points inside the images (such as /etc/prometheus) are the images' own layout, not host paths.
    """
    import re

    root = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_host_observability_install_effect"
    )
    ipv4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    host_path = re.compile(r"[\"'](?:/home/|/Users/|/data/|/mnt/|/srv/|/opt/)")
    for source in sorted(root.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        assert not ipv4.search(text), source
        assert not host_path.search(text), source
    # Positive control: the checks do see a literal.
    assert ipv4.search("x = " + ".".join(("10", "0", "0", "1")))
    assert host_path.search('x = "/data/x"')
