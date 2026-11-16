# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host row's ``volumes_root`` column: the lab host table carries it, so the row model must too.

Without the column every placement that validates the table refused the whole table
("the lab host table could not be read ... invalid: fields hosts.1.volumes_root"), the bus
placement of every host-canary lane ended ``could_not_check``, and the canary re-placed over ssh,
where a host reached only by its mDNS name was read as unreachable (piece 2, OMN-22478).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, volumes_root: str) -> dict[str, str]:
    table = tmp_path / "hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {"hosts": [{"name": "h1", "target": "h1", "volumes_root": volumes_root}]}
        ),
        encoding="utf-8",
    )
    return {"ONEX_LAB_RUN_HOSTS": str(table)}


def test_a_row_that_declares_a_volumes_root_loads_with_it(tmp_path: Path) -> None:
    table = resolve_lab_hosts_table(_write(tmp_path, "/Volumes"))
    assert table.hosts[0].volumes_root == "/Volumes"


def test_a_row_that_declares_no_volumes_root_loads_with_none(tmp_path: Path) -> None:
    table = tmp_path / "hosts.yaml"
    table.write_text(
        yaml.safe_dump({"hosts": [{"name": "h1", "target": "h1"}]}), encoding="utf-8"
    )
    loaded = resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})
    assert loaded.hosts[0].volumes_root is None


@pytest.mark.parametrize("bad", ["Volumes", "/", "//", "~/Volumes"])
def test_a_volumes_root_that_is_not_an_absolute_directory_refuses(
    tmp_path: Path, bad: str
) -> None:
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.volumes_root"):
        resolve_lab_hosts_table(_write(tmp_path, bad))


def test_the_overlay_document_with_registry_root_candidates_loads_as_the_host_table(
    tmp_path: Path,
) -> None:
    """The overlay store writes the whole lab_hosts document to the file ONEX_LAB_RUN_HOSTS names (OMN-23081).

    Read as the bare host table, the document's registry_root_candidates key refused the whole table
    ("invalid: fields ['registry_root_candidates']"), every J2 placement ended could_not_check and the
    join exited 1.
    """
    table = tmp_path / "overlay.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [{"name": "h1", "target": "h1"}],
                "registry_root_candidates": ["$HOME/registry"],
            }
        ),
        encoding="utf-8",
    )
    loaded = resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})
    assert [host.name for host in loaded.hosts] == ["h1"]


def test_an_unknown_key_beside_the_hosts_still_refuses(tmp_path: Path) -> None:
    table = tmp_path / "overlay.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [{"name": "h1", "target": "h1"}],
                "registry_root_candidates": ["$HOME/registry"],
                "stray": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match=r"stray"):
        resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})
