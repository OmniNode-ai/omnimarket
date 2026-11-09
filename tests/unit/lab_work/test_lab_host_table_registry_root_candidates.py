# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab host table's top-level ``registry_root_candidates``: the table carries it, so the table model must too.

The host-list projection built from the lab_hosts overlay holds ``registry_root_candidates`` beside ``hosts``,
``unit_needs`` and ``bus_lane``. The table model forbade extra fields, so every bus placement that read the
projection refused the whole table (``could_not_check``, invalid field ``registry_root_candidates``), and the bus
placement of lab work ended there (piece 2, OMN-22869).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table

pytestmark = pytest.mark.unit

_ROW = {"name": "h1", "target": "h1"}


def _load(tmp_path: Path, document: dict[str, object]):
    table = tmp_path / "hosts.yaml"
    table.write_text(yaml.safe_dump(document), encoding="utf-8")
    return resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(table)})


def test_a_table_that_declares_registry_root_candidates_loads_with_them(
    tmp_path: Path,
) -> None:
    roots = ["$HOME/registry-a", "/srv/registry-b"]
    loaded = _load(tmp_path, {"hosts": [_ROW], "registry_root_candidates": roots})
    assert loaded.registry_root_candidates == tuple(roots)


def test_a_table_that_declares_none_loads_with_none(tmp_path: Path) -> None:
    assert _load(tmp_path, {"hosts": [_ROW]}).registry_root_candidates is None


@pytest.mark.parametrize("bad", ["has space", "back`tick", "$(cmd)x y", ""])
def test_a_candidate_the_probe_script_cannot_quote_refuses(
    tmp_path: Path, bad: str
) -> None:
    with pytest.raises(NodeOverlayError, match=r"registry_root_candidates"):
        _load(tmp_path, {"hosts": [_ROW], "registry_root_candidates": [bad]})


def test_an_unrelated_extra_field_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match=r"not_a_table_field"):
        _load(tmp_path, {"hosts": [_ROW], "not_a_table_field": 1})
