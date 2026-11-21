# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve unit's work root is the one the host's row declares (OMN-26371)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_keys import ModelLabHostsTable
from tests.nodes.node_host_installer_serve_units.test_host_serve_units import (
    SERVE_ENV,
    Host,
    _lines,
)


def _table_with(host: Host, work_root: str | None) -> ModelLabHostsTable:
    table = host.table()
    row = table.hosts[0].model_copy(update={"lab_work_root": work_root})
    return table.model_copy(update={"hosts": (row,)})


def test_work_root_declared_reaches_the_serve_environment(tmp_path: Path) -> None:
    host = Host(tmp_path)
    declared = str(tmp_path / "data" / "lab-run")
    result = host.handler(hosts=lambda: _table_with(host, declared)).handle(
        host.request()
    )
    assert result.status == "success", result.stderr
    assert _lines(result.environments[SERVE_ENV])["LAB_WORK_ROOT"] == declared


def test_work_root_declared_is_created_on_install(tmp_path: Path) -> None:
    host = Host(tmp_path)
    declared = tmp_path / "data" / "lab-run"
    result = host.handler(hosts=lambda: _table_with(host, str(declared))).handle(
        host.request(mode="install")
    )
    assert result.status == "success", result.stderr
    assert declared.is_dir()
    assert not (host.home / "lab-run").exists()


def test_work_root_declared_absent_keeps_the_home_default(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(hosts=lambda: _table_with(host, None)).handle(host.request())
    assert (
        _lines(result.environments[SERVE_ENV])["LAB_WORK_ROOT"]
        == f"{host.home}/lab-run"
    )


@pytest.mark.parametrize(
    "bad",
    ["relative/lab-run", "/data/../etc/lab-run", "/data/lab-run; rm", "/data/work"],
)
def test_work_root_declared_must_be_an_absolute_lab_run_path(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelLabHostsTable.model_validate(
            {"hosts": [{"name": "x", "target": "x", "lab_work_root": bad}]}
        )
