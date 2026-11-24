# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_serve_units: the serve env names the host row's declared temp and cache roots (OMN-26371).

Read on h053 and h051 at 2026-11-24T09:48Z: the serve processes took ``TMPDIR`` and ``UV_CACHE_DIR`` from a per-user
environment file the host installer wrote, not from the host's row, so no declared value said which volume a lane's temp
and caches sit on. The row now declares ``lab_tmp_root`` and ``lab_cache_root``; the installer writes them into the serve
environment and refuses a root on a volume above the bar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.work_root_volume_bar import ModelVolumeReading
from omnimarket.models.overlay_keys import ModelLabHostsTable
from tests.nodes.node_host_installer_serve_units.test_host_serve_units import (
    REFRESH_ENV,
    SERVE_ENV,
    Host,
    _lines,
)

GIB = 1024**3
KEYS = ("TMPDIR", "UV_CACHE_DIR", "XDG_CACHE_HOME")


def _volume_at(used_gib: int) -> Any:
    def reader(path: Path) -> ModelVolumeReading:
        return ModelVolumeReading(
            path=str(path),
            used_bytes=used_gib * GIB,
            available_bytes=(1000 - used_gib) * GIB,
        )

    return reader


def _volume_by_path(over: Path) -> Any:
    """A reader that reports 95 percent used under ``over`` and 10 percent elsewhere."""

    def reader(path: Path) -> ModelVolumeReading:
        used = 950 if str(path).startswith(str(over)) else 100
        return ModelVolumeReading(
            path=str(path), used_bytes=used * GIB, available_bytes=(1000 - used) * GIB
        )

    return reader


def _table_with(host: Host, **keys: str) -> Any:
    def table() -> ModelLabHostsTable:
        base = host.table().model_dump(mode="json", exclude_none=True)
        base["hosts"][0].update(keys)
        return ModelLabHostsTable.model_validate(base)

    return table


def test_temp_cache_roots_declared_serve_env_names_the_declared_roots(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tmp_root = str(tmp_path / "nvme" / "lab-run" / ".tmp")
    cache_root = str(tmp_path / "nvme" / "lab-run" / ".cache")
    result = host.handler(
        hosts=_table_with(host, lab_tmp_root=tmp_root, lab_cache_root=cache_root),
        volume=_volume_at(300),
    ).handle(host.request())
    assert result.status == "success", result.stderr
    serve = _lines(result.environments[SERVE_ENV])
    assert serve["TMPDIR"] == tmp_root
    assert serve["UV_CACHE_DIR"] == f"{cache_root}/uv"
    assert serve["XDG_CACHE_HOME"] == cache_root
    refresh = _lines(result.environments[REFRESH_ENV])
    assert not [key for key in KEYS if key in refresh]


def test_temp_cache_roots_declared_a_row_without_them_writes_none(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    result = host.handler(volume=_volume_at(300)).handle(host.request())
    assert result.status == "success", result.stderr
    serve = _lines(result.environments[SERVE_ENV])
    assert not [key for key in KEYS if key in serve]


def test_temp_cache_roots_declared_the_cache_alone_leaves_temp_unset(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    cache_root = str(tmp_path / "nvme" / ".cache")
    result = host.handler(
        hosts=_table_with(host, lab_cache_root=cache_root), volume=_volume_at(300)
    ).handle(host.request())
    assert result.status == "success", result.stderr
    serve = _lines(result.environments[SERVE_ENV])
    assert "TMPDIR" not in serve
    assert serve["UV_CACHE_DIR"] == f"{cache_root}/uv"


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("lab_tmp_root", "tmp-root-volume-over-bar"),
        ("lab_cache_root", "cache-root-volume-over-bar"),
    ],
)
def test_temp_cache_roots_volume_bar_refuses_a_root_above_the_bar(
    tmp_path: Path, key: str, reason: str
) -> None:
    host = Host(tmp_path)
    full = tmp_path / "full" / "lab-run"
    result = host.handler(
        hosts=_table_with(host, **{key: str(full / ".x")}),
        volume=_volume_by_path(full),
    ).handle(host.request())
    assert result.status == "refused"
    assert result.reason == reason


def test_temp_cache_roots_volume_bar_admits_a_root_at_the_bar(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.handler(
        hosts=_table_with(
            host,
            lab_tmp_root=str(tmp_path / "nvme" / ".tmp"),
            lab_cache_root=str(tmp_path / "nvme" / ".cache"),
        ),
        volume=_volume_at(850),
    ).handle(host.request())
    assert result.status == "success", result.stderr


@pytest.mark.parametrize("key", ["lab_tmp_root", "lab_cache_root"])
@pytest.mark.parametrize("value", ["relative/path", "/tmp/with space", "/"])
def test_temp_cache_roots_declared_model_refuses_a_malformed_root(
    tmp_path: Path, key: str, value: str
) -> None:
    host = Host(tmp_path)
    with pytest.raises(ValidationError):
        _table_with(host, **{key: value})()
