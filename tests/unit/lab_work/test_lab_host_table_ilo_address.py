# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host row's ``ilo_address`` column: the lab host table carries it, so the row model must too.

The packaged lab_hosts overlay declares the iLO management address of the hosts that have one
(OMN-22870). The row model refuses an unknown column, so without this field every reader that
validated the table refused it whole ("invalid for lab_hosts: fields hosts.7.ilo_address,
hosts.8.ilo_address"), ``onex lab-run --via bus`` ended ``could_not_check`` and exited 75 for every
lane (OMN-22878).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, row: dict[str, object]) -> dict[str, str]:
    table = tmp_path / "hosts.yaml"
    table.write_text(
        yaml.safe_dump({"hosts": [{"name": "h1", "target": "h1", **row}]}),
        encoding="utf-8",
    )
    return {"ONEX_LAB_RUN_HOSTS": str(table)}


@pytest.mark.parametrize("good", ["203.0.113.10", "ilo-h1.lab.example", "fe80::1"])
def test_a_row_that_declares_an_ilo_address_loads_with_it(
    tmp_path: Path, good: str
) -> None:
    table = resolve_lab_hosts_table(_write(tmp_path, {"ilo_address": good}))
    assert table.hosts[0].ilo_address == good


def test_a_row_that_declares_no_ilo_address_loads_with_none(tmp_path: Path) -> None:
    table = resolve_lab_hosts_table(_write(tmp_path, {}))
    assert table.hosts[0].ilo_address is None


@pytest.mark.parametrize(
    "bad", ["", " ", "https://203.0.113.10", "203.0.113.10/redfish", "ilo h1", "-ilo"]
)
def test_an_ilo_address_that_is_not_a_bare_host_refuses(
    tmp_path: Path, bad: str
) -> None:
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.ilo_address"):
        resolve_lab_hosts_table(_write(tmp_path, {"ilo_address": bad}))


def test_an_unknown_column_still_refuses(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.ilo_adress"):
        resolve_lab_hosts_table(_write(tmp_path, {"ilo_adress": "203.0.113.10"}))
