# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosts overlay's shape and the settings node's overlay seams (OMN-24059).

Seams measured here: (1) the hosts overlay document the overlays repository holds, as a model; (2) the
sibling loader node's overlay (its example file loads through this node); (3) the overlay root lookup, which
must go through ``node_overlay_reader.find_node_overlay`` so the one overlay root resolver's answer is the
roots; (4) the keys the CLI prints, which the plugin's drain_map reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers import node_overlay_reader
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_ledger_rows_loader_effect import (
    overlay as loader_overlay_module,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.overlay import (
    load_hosts_overlay,
    load_loader_overlay,
)
from tests.unit.nodes.node_ledger_rows_loader_settings_effect.conftest_support import (
    HEAD,
    host_row,
    write_overlays,
)

pytestmark = pytest.mark.unit

# The hosts overlay as overlays/ledger_rows_loader_hosts/overlay.yaml holds it (the overlays repository's pin
# loads the real file through this node's model; this copy is the seam the two sides must keep).
REAL_SHAPE = """\
# comment lines are ignored
schema_version: ledger_rows_loader_hosts.v1
timeout_seconds: 120
hosts:
  - {"host": "mac-host", "command": ["{registry_root}/.onex-dispatch-venv/bin/python", "-m", "omnimarket.nodes.node_ledger_rows_loader_effect"]}
  - {"host": "hp-gen8", "command": ["{registry_root}/.onex-dispatch-venv/bin/python", "-m", "omnimarket.nodes.node_ledger_rows_loader_effect"]}
"""


def test_the_overlays_repository_shape_loads_as_a_model(tmp_path: Path) -> None:
    env = write_overlays(tmp_path / "ovl", hosts=REAL_SHAPE)
    overlay = load_hosts_overlay(env)
    assert overlay.timeout_seconds == 120
    assert [row.host for row in overlay.hosts] == ["mac-host", "hp-gen8"]


def test_a_hosts_overlay_with_no_rows_loads_as_an_empty_tuple(tmp_path: Path) -> None:
    env = write_overlays(tmp_path / "ovl", hosts=HEAD)
    assert load_hosts_overlay(env).hosts == ()


def test_the_sibling_loader_example_overlay_loads_through_this_node(
    tmp_path: Path,
) -> None:
    example = Path(loader_overlay_module.__file__).with_name("overlay.example.yaml")
    root = tmp_path / "ovl" / "node_ledger_rows_loader_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(example.read_text())
    assert load_loader_overlay(
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "ovl")}
    ).file_fallback_allowed in (True, False)


def test_the_overlay_lookup_goes_through_find_node_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver seam: roots are whatever find_node_overlay answers, not a second root walk."""
    elsewhere = tmp_path / "resolved"
    write_overlays(elsewhere)
    wanted: list[str] = []

    def answer(node_name: str, environ: object = None) -> Path:
        wanted.append(node_name)
        return elsewhere / node_name / "overlay.yaml"

    monkeypatch.setattr(node_overlay_reader, "find_node_overlay", answer)
    # the declared roots name nothing: only the resolver's answer can find the overlay
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "declared-empty")}
    assert load_hosts_overlay(env).timeout_seconds == 45
    assert load_loader_overlay(env).file_fallback_allowed is True
    assert wanted == ["ledger_rows_loader_hosts", "node_ledger_rows_loader_effect"]


def test_failing_control_without_the_resolver_answer_the_overlay_is_not_found(
    tmp_path: Path,
) -> None:
    write_overlays(tmp_path / "resolved")
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "declared-empty")}
    with pytest.raises(NodeOverlayError, match="ledger_rows_loader_hosts"):
        load_hosts_overlay(env)


@pytest.mark.parametrize(
    ("hosts", "fields"),
    [
        (HEAD + host_row() + "extra: 1\n", "<document>|hosts"),
        (
            "schema_version: ledger_rows_loader_hosts.v1\ntimeout_seconds: 45\nextra: 1\nhosts:\n"
            + host_row(),
            "extra",
        ),
        (HEAD.replace("v1", "v0") + host_row(), "schema_version"),
        (HEAD.replace("45", "0") + host_row(), "timeout_seconds"),
        (HEAD.replace("45", "true") + host_row(), "timeout_seconds"),
        (HEAD.replace("45", "601") + host_row(), "timeout_seconds"),
        (HEAD.replace("45", "'45'") + host_row(), "timeout_seconds"),
        (
            HEAD
            + "  - "
            + json.dumps({"host": "hp-gen8", "command": ["x"], "extra": 1})
            + "\n",
            "hosts.0.extra",
        ),
        (HEAD + host_row(command=[]), "hosts.0.command"),
        (HEAD + host_row(command=["x", ""]), "hosts.0.command.1"),
        (HEAD + host_row(host=""), "hosts.0.host"),
        (HEAD + host_row() + host_row(host="HP-GEN8"), "hosts"),
        (HEAD + "  - not-json\n", "hosts"),
        ("- a\n- b\n", "<document>"),
    ],
)
def test_error_chain_a_malformed_hosts_overlay_is_refused_naming_it_and_the_fields(
    tmp_path: Path, hosts: str, fields: str
) -> None:
    env = write_overlays(tmp_path / "ovl", hosts=hosts)
    with pytest.raises(NodeOverlayError, match="ledger_rows_loader_hosts") as raised:
        load_hosts_overlay(env)
    assert any(f in str(raised.value) for f in fields.split("|"))


def test_error_chain_the_loader_flag_that_is_not_a_bool_is_refused(
    tmp_path: Path,
) -> None:
    env = write_overlays(tmp_path / "ovl", allowed="maybe")
    with pytest.raises(NodeOverlayError, match="file_fallback_allowed"):
        load_loader_overlay(env)
