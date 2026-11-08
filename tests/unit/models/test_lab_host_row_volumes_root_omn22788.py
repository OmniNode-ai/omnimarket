# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host row model's ``volumes_root`` column, read the way the acceptance criteria name it.

AC1 is the row model itself (``ModelLabHostRow``); the ``packaged_table`` test is AC2, the packaged
host table's shape (an ``h101`` row with a ``volumes_root`` cell) loaded through the host table loader.
The table is written inline so the check runs on every runner, whether or not the packaged file is
checked out beside this repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow

pytestmark = pytest.mark.unit


def test_a_row_with_an_absolute_volumes_root_loads() -> None:
    row = ModelLabHostRow(name="h101", target="h101", volumes_root="/Volumes")
    assert row.volumes_root == "/Volumes"


def test_a_row_without_a_volumes_root_still_loads() -> None:
    row = ModelLabHostRow(name="h053", target="h053")
    assert row.volumes_root is None


@pytest.mark.parametrize("bad", ["Volumes", "./Volumes", "~/Volumes", "/", "//"])
def test_a_volumes_root_that_is_not_an_absolute_directory_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="volumes_root"):
        ModelLabHostRow(name="h101", target="h101", volumes_root=bad)


def test_packaged_table_with_the_h101_volumes_root_cell_loads_whole(
    tmp_path: Path,
) -> None:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "h053", "target": "h053"},
                    {"name": "h101", "target": "h101", "volumes_root": "/Volumes"},
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})
    assert [(row.name, row.volumes_root) for row in loaded.hosts] == [
        ("h053", None),
        ("h101", "/Volumes"),
    ]


def test_packaged_table_with_a_relative_volumes_root_is_refused_naming_the_cell(
    tmp_path: Path,
) -> None:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "h053", "target": "h053"},
                    {"name": "h101", "target": "h101", "volumes_root": "Volumes"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match=r"hosts\.1\.volumes_root"):
        resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})
