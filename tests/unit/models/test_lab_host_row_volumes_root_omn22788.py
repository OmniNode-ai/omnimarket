# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab host row accepts volumes_root, so the host table with the h101 volume mount root loads (OMN-22801)."""

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)

pytestmark = pytest.mark.unit

_ROW = {"name": "host-a", "target": "host-a"}

# The shape of the h101 row of the packaged table as OMN-22688 shipped it: a Mac that mounts its external volume under /Volumes.
_H101_ROW = {
    "name": "h101",
    "target": "h101.example.invalid",
    "ledger_path": "$OMNI_HOME/docs/tracking/ROLLING_WORK_LEDGER.md",
    "volumes_root": "/Volumes",
}


def test_an_absolute_volumes_root_loads() -> None:
    row = ModelLabHostRow.model_validate({**_ROW, "volumes_root": "/Volumes"})
    assert row.volumes_root == "/Volumes"


def test_a_row_without_volumes_root_still_loads() -> None:
    assert ModelLabHostRow.model_validate(_ROW).volumes_root is None


@pytest.mark.parametrize("value", ["Volumes", "/", "//", "./Volumes"])
def test_a_relative_or_root_volumes_root_is_refused(value: str) -> None:
    with pytest.raises(
        ValidationError, match=r"string_pattern_mismatch|filesystem root"
    ):
        ModelLabHostRow.model_validate({**_ROW, "volumes_root": value})


def test_packaged_table() -> None:
    """A host table holding the h101 volumes_root cell loads whole.

    The table is the h101 row shape of the packaged table, spelled here so the check runs on every runner. Where
    ONEX_LAB_RUN_HOSTS names a table file, the placement readers' own file is loaded too and must carry the cell.
    """
    tables = [{"hosts": [_ROW, _H101_ROW]}]
    named = os.environ.get("ONEX_LAB_RUN_HOSTS", "")
    if named and Path(named).is_file():
        document = yaml.safe_load(Path(named).read_text())
        tables.append({"hosts": list(document["hosts"])})
    for raw in tables:
        table = ModelLabHostsTable.model_validate(raw)
        roots = {row.name: row.volumes_root for row in table.hosts}
        assert roots["h101"] == "/Volumes"
