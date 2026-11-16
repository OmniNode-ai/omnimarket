# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_serve_units: extra serve units on other bus lanes (OMN-24131)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_serve_units.handlers.handler_serve_units import (
    NODE_DIR,
)
from omnimarket.nodes.node_host_installer_serve_units.models.model_serve_units import (
    ModelServeUnitsOverlay,
)
from tests.nodes.node_host_installer_serve_units.test_host_serve_units import (
    REFRESH_ENV,
    SERVE_ENV,
    Host,
    _lines,
    _overlay,
)

LANE_ENV = "serve-c.env"
LANE_UNIT = "unit-c.service"


def _lane_overlay(roots: dict[str, list[str]]) -> ModelServeUnitsOverlay:
    base = _overlay(roots).model_dump()
    base["lane_serves"] = [
        {
            "name": LANE_UNIT,
            "ticket": "OMN-10003",
            "env_file": f".config/app/{LANE_ENV}",
            "log_directory": ".local/state/app/serve-c",
            "import_probe": "pkg.serve_a",
            "bus_lane": "other-lane",
            "work_directory": "work-other",
        }
    ]
    return ModelServeUnitsOverlay.model_validate(base)


def test_dev_lane_no_lane_serves_renders_only_the_two_env_files(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler().handle(host.request())
    assert result.status == "success", result.stderr
    assert set(result.environments) == {SERVE_ENV, REFRESH_ENV}


def test_dev_lane_one_lane_serve_renders_a_third_env_file(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(overlay=lambda: _lane_overlay(host.roots)).handle(
        host.request()
    )
    assert result.status == "success", result.stderr
    assert set(result.environments) == {SERVE_ENV, REFRESH_ENV, LANE_ENV}
    lane = _lines(result.environments[LANE_ENV])
    assert lane["LAB_WORK_BUS_LANE"] == "other-lane"
    assert lane["LAB_WORK_ROOT"].endswith("/work-other")
    assert (
        lane["LAB_WORK_HOST_NAME"]
        == _lines(result.environments[SERVE_ENV])["LAB_WORK_HOST_NAME"]
    )


def test_dev_lane_golden_chain_install_writes_and_starts_the_lane_serve(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    handler = host.handler(overlay=lambda: _lane_overlay(host.roots))
    result = handler.handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    assert (host.home / ".config/app" / LANE_ENV).is_file()
    assert (host.home / ".config/systemd/user" / LANE_UNIT).is_file()
    assert (host.home / "work-other").is_dir()
    systemctl = [c[2:] for c in host.commands if c[:2] == ["systemctl", "--user"]]
    assert systemctl.index(["enable", LANE_UNIT]) > systemctl.index(
        ["restart", "unit-b.service"]
    )
    host.commands.clear()
    host.active = "active\n"
    again = handler.handle(host.request(mode="install"))
    assert again.status == "success"
    assert again.changed is False


def test_dev_lane_uninstall_removes_the_lane_serve(tmp_path: Path) -> None:
    host = Host(tmp_path)
    handler = host.handler(overlay=lambda: _lane_overlay(host.roots))
    handler.handle(host.request(mode="install"))
    result = handler.handle(host.request(mode="uninstall"))
    assert result.status == "success"
    assert not (host.home / ".config/systemd/user" / LANE_UNIT).exists()
    assert not (host.home / ".config/app" / LANE_ENV).exists()


def test_dev_lane_lane_serve_on_the_table_bus_lane_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    base = _overlay(host.roots).model_dump()
    base["lane_serves"] = [
        {
            "name": LANE_UNIT,
            "ticket": "OMN-10003",
            "env_file": f".config/app/{LANE_ENV}",
            "log_directory": ".local/state/app/serve-c",
            "import_probe": "pkg.serve_a",
            "bus_lane": "test-lane",
            "work_directory": "work-other",
        }
    ]
    result = host.handler(
        overlay=lambda: ModelServeUnitsOverlay.model_validate(base)
    ).handle(host.request(mode="install"))
    assert result.status == "refused", result.stderr
    assert result.reason == "lane-serve-duplicates-table-lane"
    assert result.exit_code == 2
    assert not (host.home / ".config").exists()


def test_dev_lane_repeated_unit_name_is_a_validation_error(tmp_path: Path) -> None:
    base = _overlay({"pool-a": ["$OMNI_HOME/plugin-overlays"]}).model_dump()
    base["lane_serves"] = [
        {
            "name": "unit-a.service",
            "ticket": "OMN-10003",
            "env_file": f".config/app/{LANE_ENV}",
            "log_directory": ".local/state/app/serve-c",
            "import_probe": "pkg.serve_a",
            "bus_lane": "other-lane",
            "work_directory": "work-other",
        }
    ]
    with pytest.raises(ValueError, match="a unit name is declared more than once"):
        ModelServeUnitsOverlay.model_validate(base)


def test_dev_lane_repeated_env_file_base_name_is_a_validation_error(
    tmp_path: Path,
) -> None:
    base = _overlay({"pool-a": ["$OMNI_HOME/plugin-overlays"]}).model_dump()
    base["lane_serves"] = [
        {
            "name": LANE_UNIT,
            "ticket": "OMN-10003",
            "env_file": f".config/app/{SERVE_ENV}",
            "log_directory": ".local/state/app/serve-c",
            "import_probe": "pkg.serve_a",
            "bus_lane": "other-lane",
            "work_directory": "work-other",
        }
    ]
    with pytest.raises(
        ValueError, match="an environment file base name is declared more than once"
    ):
        ModelServeUnitsOverlay.model_validate(base)


def test_dev_lane_repeated_log_directory_is_a_validation_error(tmp_path: Path) -> None:
    base = _overlay({"pool-a": ["$OMNI_HOME/plugin-overlays"]}).model_dump()
    base["lane_serves"] = [
        {
            "name": LANE_UNIT,
            "ticket": "OMN-10003",
            "env_file": f".config/app/{LANE_ENV}",
            "log_directory": ".local/state/app/serve-a",
            "import_probe": "pkg.serve_a",
            "bus_lane": "other-lane",
            "work_directory": "work-other",
        }
    ]
    with pytest.raises(ValueError, match="a log directory is declared more than once"):
        ModelServeUnitsOverlay.model_validate(base)


def test_seam_dev_lane_the_unit_template_reads_only_variables_the_lane_serve_env_defines(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler(overlay=lambda: _lane_overlay(host.roots)).handle(
        host.request()
    )
    assert result.status == "success", result.stderr
    text = (NODE_DIR / "templates" / "serve.service.tmpl").read_text()
    exec_line = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    names = set(re.findall(r"\$\{([A-Z_]+)\}", exec_line)) | set(
        re.findall(r"(?<!\$\{)(?<!\w)\$([A-Z_]+)", exec_line)
    )
    names.discard("LAB_WORK_MAX_UNITS")
    defined = _lines(result.environments[LANE_ENV])
    for name in names:
        assert name in defined, f"{name} is not defined in the lane serve env"
    if _lines(result.environments[SERVE_ENV]).get("LAB_WORK_MAX_UNITS"):
        assert "LAB_WORK_MAX_UNITS" in defined


def test_seam_dev_lane_the_serve_cli_has_the_bus_lane_option_the_template_passes(
    tmp_path: Path,
) -> None:
    from omnimarket.lab_work.cli import serve_command

    params = {p.name: p for p in serve_command.params}
    assert "bus_lane" in params
    text = (NODE_DIR / "templates" / "serve.service.tmpl").read_text()
    assert "--bus-lane" in text
