# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observability install (OMN-26657): a textfile directory per target, and the launchd exporter for a Mac host."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_observability_overlay import (
    ModelObservabilityOverlay,
)
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_host_observability_install_effect.handlers.handler_observability_install import (
    HandlerObservabilityInstall,
)
from omnimarket.nodes.node_host_observability_install_effect.handlers.render_observability import (
    lint_launchd_plist,
    render_container_units_overlay,
    render_launchd_plist,
    units_for_host,
)
from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityInstallRequest,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_host_observability_install_effect/overlay.example.yaml"
)


def _document(tmp_path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["config_root"] = str(tmp_path / "config")
    document["exporter"]["textfile_directory"] = "/var/lib/example/textfile"
    return document


def _with_mac(document: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    document["targets"]["host-m"] = {
        "address": "host-m.example.test",
        "exporter": "launchd",
        "port": 9100,
    }
    document["launchd_exporter"] = {
        "label": "test.example.node-exporter",
        "binary": "/opt/example/bin/node_exporter",
        "plist_directory": str(tmp_path / "LaunchDaemons"),
        "log_directory": "/var/log/example",
        "textfile_directory": "/opt/example/textfile",
        "domain": "system",
        "enable_collectors": ["textfile"],
    }
    return document


def _table(names: Sequence[str]) -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(
        {"hosts": [{"name": n, "target": n} for n in names]}
    )


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "ok\n", "")


def _handler(names: Sequence[str], run: Recorder) -> HandlerObservabilityInstall:
    return HandlerObservabilityInstall(
        run=run,
        hosts=lambda _file: _table(names),
        python="/usr/bin/python-test",
        environ={},
    )


def _write(tmp_path: Path, document: dict[str, Any]) -> str:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


def _volumes(overlay: ModelObservabilityOverlay, host: str) -> list[str]:
    units = render_container_units_overlay(overlay, host)["units"]
    return list(units[0]["create"]["volumes"])


def test_two_install_hosts_with_different_textfile_directories_mount_their_own(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    document["targets"]["host-b"]["exporter"] = "install"
    document["targets"]["host-b"]["textfile_directory"] = "/srv/other/textfile"
    overlay = ModelObservabilityOverlay.model_validate(document)
    assert "/var/lib/example/textfile:/textfile:ro" in _volumes(overlay, "host-a")
    assert "/srv/other/textfile:/textfile:ro" in _volumes(overlay, "host-b")
    assert not any(
        "/var/lib/example/textfile" in v for v in _volumes(overlay, "host-b")
    )


def test_install_creates_the_directory_of_the_host_it_runs_on(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["targets"]["host-a"]["textfile_directory"] = str(tmp_path / "own")
    document["exporter"]["textfile_directory"] = str(tmp_path / "shared-default")
    result = _handler(["host-a", "host-b"], Recorder()).handle(
        ModelObservabilityInstallRequest(
            host_name="host-a", mode="install", overlay_file=_write(tmp_path, document)
        )
    )
    assert result.exit_code == 0
    assert (tmp_path / "own").is_dir()
    assert not (tmp_path / "shared-default").exists()


def test_a_relative_per_target_textfile_directory_is_refused(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["targets"]["host-a"]["textfile_directory"] = "relative/dir"
    with pytest.raises(ValidationError):
        ModelObservabilityOverlay.model_validate(document)


def test_a_launchd_target_needs_the_launchd_exporter_section(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["targets"]["host-m"] = {
        "address": "host-m.example.test",
        "exporter": "launchd",
        "port": 9100,
    }
    with pytest.raises(ValidationError, match="launchd_exporter"):
        ModelObservabilityOverlay.model_validate(document)


def test_the_launchd_plist_is_rendered_from_the_settings_and_lints_clean(
    tmp_path: Path,
) -> None:
    overlay = ModelObservabilityOverlay.model_validate(
        _with_mac(_document(tmp_path), tmp_path)
    )
    text = render_launchd_plist(overlay, "host-m")
    assert lint_launchd_plist(text, overlay) == []
    assert "--web.listen-address=host-m.example.test:9100" in text
    assert "--collector.textfile.directory=/opt/example/textfile" in text
    assert units_for_host(overlay, "host-m") == ["test.example.node-exporter"]
    assert render_container_units_overlay(overlay, "host-m") == {"units": []}


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda t: t.replace("test.example.node-exporter", "wrong.label"), "Label"),
        (
            lambda t: t.replace("/opt/example/bin/node_exporter", "node_exporter"),
            "absolute",
        ),
        (lambda t: t.replace("<key>RunAtLoad</key>", "<key>Other</key>"), "RunAtLoad"),
        (lambda t: t.replace("</dict>\n</plist>", ""), "not a property list"),
    ],
)
def test_a_bad_plist_fails_the_lint(tmp_path: Path, mutate: Any, needle: str) -> None:
    overlay = ModelObservabilityOverlay.model_validate(
        _with_mac(_document(tmp_path), tmp_path)
    )
    problems = lint_launchd_plist(
        mutate(render_launchd_plist(overlay, "host-m")), overlay
    )
    assert any(needle in p for p in problems), problems


def test_dry_run_for_a_mac_host_prints_the_plist_and_installs_nothing(
    tmp_path: Path,
) -> None:
    run = Recorder()
    overlay_file = _write(tmp_path, _with_mac(_document(tmp_path), tmp_path))
    result = _handler(["host-a", "host-b", "host-m"], run).handle(
        ModelObservabilityInstallRequest(
            host_name="host-m", mode="dry_run", overlay_file=overlay_file
        )
    )
    assert result.exit_code == 0
    assert "test.example.node-exporter.plist" in result.stdout
    assert "LINT OK" in result.stdout
    assert run.calls == []
    assert not (tmp_path / "LaunchDaemons").exists()


def test_install_for_a_mac_host_writes_the_plist_and_bootstraps_it(
    tmp_path: Path,
) -> None:
    run = Recorder()
    overlay_file = _write(tmp_path, _with_mac(_document(tmp_path), tmp_path))
    result = _handler(["host-a", "host-b", "host-m"], run).handle(
        ModelObservabilityInstallRequest(
            host_name="host-m", mode="install", overlay_file=overlay_file
        )
    )
    assert result.exit_code == 0
    plist = tmp_path / "LaunchDaemons" / "test.example.node-exporter.plist"
    assert plist.is_file()
    assert run.calls[-1] == ["launchctl", "bootstrap", "system", str(plist)]


def test_a_mac_host_marked_contributor_is_never_a_target(tmp_path: Path) -> None:
    run = Recorder()
    table = ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {"name": "host-a", "target": "host-a"},
                {"name": "host-b", "target": "host-b"},
                {
                    "name": "host-m",
                    "target": "host-m",
                    "contributor": True,
                    "lane_binding": "example-lane",
                    "lane_not_ready_reason": "owned by its owner",
                },
            ]
        }
    )
    handler = HandlerObservabilityInstall(
        run=run, hosts=lambda _f: table, python="/usr/bin/python-test", environ={}
    )
    result = handler.handle(
        ModelObservabilityInstallRequest(
            host_name="host-m",
            mode="install",
            overlay_file=_write(tmp_path, _with_mac(_document(tmp_path), tmp_path)),
        )
    )
    assert result.exit_code == 1
    assert run.calls == []
    assert not (tmp_path / "LaunchDaemons").exists()
