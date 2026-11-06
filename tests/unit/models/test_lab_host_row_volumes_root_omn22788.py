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


def test_an_absolute_volumes_root_loads() -> None:
    row = ModelLabHostRow.model_validate({**_ROW, "volumes_root": "/Volumes"})
    assert row.volumes_root == "/Volumes"


def test_a_row_without_volumes_root_still_loads() -> None:
    assert ModelLabHostRow.model_validate(_ROW).volumes_root is None


@pytest.mark.parametrize("value", ["Volumes", "/", "//", "./Volumes"])
def test_a_relative_or_root_volumes_root_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="absolute directory"):
        ModelLabHostRow.model_validate({**_ROW, "volumes_root": value})


def test_packaged_table() -> None:
    """The packaged lab host table loads with its h101 volumes_root cell.

    The table file is the one ONEX_LAB_RUN_HOSTS names, the same file the placement readers load; the test is
    skipped where the variable is unset, and the lab run of this ticket sets it to the packaged table.
    """
    named = os.environ.get("ONEX_LAB_RUN_HOSTS", "")
    path = Path(named)
    if not named or not path.is_file():
        pytest.skip("ONEX_LAB_RUN_HOSTS does not name a table file")
    document = yaml.safe_load(path.read_text())
    table = ModelLabHostsTable.model_validate({"hosts": list(document["hosts"])})
    roots = {row.name: row.volumes_root for row in table.hosts}
    assert roots["h101"] == "/Volumes"
