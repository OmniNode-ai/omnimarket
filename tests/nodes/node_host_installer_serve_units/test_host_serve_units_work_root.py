# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_serve_units: the serve unit works under the host row's declared lane work root (OMN-26371)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.work_root_volume_bar import ModelVolumeReading
from omnimarket.models.overlay_keys import ModelLabHostsTable
from tests.nodes.node_host_installer_serve_units.test_host_serve_units import (
    REFRESH_ENV,
    SERVE_ENV,
    Host,
    _lines,
)

GIB = 1024**3


def _volume_at(used_gib: int) -> Any:
    """A reader whose volume is ``used_gib`` of 1000 GiB used, whatever the path."""

    def reader(path: Path) -> ModelVolumeReading:
        return ModelVolumeReading(
            path=str(path),
            used_bytes=used_gib * GIB,
            available_bytes=(1000 - used_gib) * GIB,
        )

    return reader


def _table_with_root(host: Host, root: str | None) -> Any:
    def table() -> ModelLabHostsTable:
        base = host.table().model_dump(mode="json", exclude_none=True)
        if root is not None:
            base["hosts"][0]["lab_work_root"] = root
        return ModelLabHostsTable.model_validate(base)

    return table


def test_work_root_declared_serve_env_names_the_rows_root(tmp_path: Path) -> None:
    host = Host(tmp_path)
    data_root = str(tmp_path / "data" / "lab-run")
    result = host.handler(
        hosts=_table_with_root(host, data_root), volume=_volume_at(300)
    ).handle(host.request())
    assert result.status == "success", result.stderr
    assert _lines(result.environments[SERVE_ENV])["LAB_WORK_ROOT"] == data_root
    assert "LAB_WORK_ROOT" not in _lines(result.environments[REFRESH_ENV])


def test_work_root_declared_a_row_without_one_keeps_the_home_directory_root(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler(volume=_volume_at(300)).handle(host.request())
    assert result.status == "success", result.stderr
    assert (
        _lines(result.environments[SERVE_ENV])["LAB_WORK_ROOT"]
        == f"{host.home}/lab-run"
    )


def test_work_root_declared_install_creates_the_declared_root(tmp_path: Path) -> None:
    host = Host(tmp_path)
    data_root = tmp_path / "data" / "lab-run"
    result = host.handler(
        hosts=_table_with_root(host, str(data_root)), volume=_volume_at(300)
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert data_root.is_dir()


def test_work_root_volume_bar_refuses_an_install_on_a_volume_above_the_bar(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    data_root = str(tmp_path / "data" / "lab-run")
    result = host.handler(
        hosts=_table_with_root(host, data_root), volume=_volume_at(860)
    ).handle(host.request(mode="install"))
    assert result.status == "refused"
    assert result.reason == "work-root-volume-over-bar"
    assert "86.0 percent used" in result.stderr
    assert not (tmp_path / "data").exists()
    assert not any(argv[0] == "systemctl" for argv in host.commands)


def test_work_root_volume_bar_admits_an_install_at_the_bar(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(
        hosts=_table_with_root(host, str(tmp_path / "data" / "lab-run")),
        volume=_volume_at(850),
    ).handle(host.request(mode="install"))
    assert result.status == "success", result.stderr


def test_work_root_volume_bar_judges_the_home_directory_root_too(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler(volume=_volume_at(910)).handle(host.request())
    assert result.status == "refused"
    assert result.reason == "work-root-volume-over-bar"


@pytest.mark.parametrize("mode", ["dry-run", "install"])
def test_work_root_volume_bar_reads_the_live_volume_by_default(
    tmp_path: Path, mode: str
) -> None:
    """With no reader injected the live volume under the root is read, so a temporary directory's volume decides."""
    host = Host(tmp_path)
    result = host.handler().handle(host.request(mode=mode))
    assert result.status in {"success", "refused"}
    if result.status == "refused":
        assert result.reason == "work-root-volume-over-bar"
