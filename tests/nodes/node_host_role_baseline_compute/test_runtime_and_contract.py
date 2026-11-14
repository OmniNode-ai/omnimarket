# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime module reads the host table and the node's overlay, and the contract names a handler that imports (OMN-24263)."""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.overlay_keys import LAB_HOSTS_FILE_ENV
from omnimarket.nodes.node_host_role_baseline_compute.runtime_host_role_baseline import (
    EXIT_REFUSED,
    main,
)

from .support import NODE

pytestmark = pytest.mark.unit


def _environ(tmp_path: Path, hosts: list[dict[str, str]]) -> dict[str, str]:
    root = tmp_path / "overlays"
    (root / "node_host_role_baseline_compute").mkdir(parents=True)
    shutil.copy(
        NODE / "overlay.example.yaml",
        root / "node_host_role_baseline_compute" / "overlay.yaml",
    )
    table = tmp_path / "hosts.yaml"
    table.write_text(yaml.safe_dump({"hosts": hosts}), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root), LAB_HOSTS_FILE_ENV: str(table)}


def test_a_host_with_a_role_prints_its_expected_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(
        tmp_path, [{"name": "h1", "target": "h1", "host_role": "lab-worker"}]
    )
    assert main(["--host", "h1", "--omni-home", "/srv/registry"], environ) == 0
    assert "ROLE h1 role=lab-worker units=3 clones=1" in capsys.readouterr().out


def test_a_host_with_no_role_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(tmp_path, [{"name": "h1", "target": "h1"}])
    assert (
        main(["--host", "h1", "--omni-home", "/srv/registry"], environ) == EXIT_REFUSED
    )
    assert "REFUSED h1: no-role" in capsys.readouterr().out


def test_a_host_with_no_row_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ(
        tmp_path, [{"name": "h1", "target": "h1", "host_role": "lab-worker"}]
    )
    assert (
        main(["--host", "h9", "--omni-home", "/srv/registry"], environ) == EXIT_REFUSED
    )
    assert "no row in the host table" in capsys.readouterr().out


def test_the_contract_names_a_handler_that_imports_and_the_overlay_it_reads() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert hasattr(module, contract["handler"]["class"])
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.host-role-baseline-derived.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert contract["overlay"]["node"] == "node_host_role_baseline_compute"
    for test in contract["dod_evidence"]:
        assert (NODE.parents[3] / test["test"]).is_file(), test["test"]
