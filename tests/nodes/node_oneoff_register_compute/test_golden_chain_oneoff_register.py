# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the one-off register read (OMN-19998): ``NodeOneOffRegisterCompute`` over ``HandlerOneOffRegister``.

The register and the liveness rows are deployment overlays under a temporary root, and the node
lists the nodes that exist itself. The golden chain reads a passing register and prints its JSON
with the correlation id given. The error chains are named breaks of that read: an unregistered
in-scope process fails the read by name, and a missing overlay refuses the read on stderr.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_oneoff_register_compute.runtime_oneoff_register import main
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

POLICY: dict[str, object] = {
    "scope_hosts": ["example-host"],
    "scope_states": ["active"],
    "entries": [
        {
            "one_off_id": "example-one-off",
            "what": "The start-path census that reads the paths once a day",
            "where": "example-host",
            "category": "C2",
            "owner": "example-owner",
            "owner_repo": "example-repo",
            "state": "replacement_live",
            "replacement_kind": "node",
            "replacement_ref": "node_start_path_census_compute",
            "bypass": "no",
            "liveness_process_id": "example-process",
            "basis": "the register of one-offs",
        }
    ],
    "exclusions": [],
}

LIVENESS: dict[str, object] = {
    "processes": [
        {"process_id": "example-process", "host": "example-host", "state": "active"}
    ]
}


def roots(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def install(
    monkeypatch: pytest.MonkeyPatch, root: Path, policy: dict[str, object] = POLICY
) -> None:
    install_node_overlay(monkeypatch, root, "node_oneoff_register_compute", policy)
    install_node_overlay(monkeypatch, root, "automation_liveness", LIVENESS)


def test_golden_chain_reads_a_passing_register_and_names_its_correlation_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(monkeypatch, tmp_path)
    code = main(["--correlation-id", "golden-1"], environ=roots(tmp_path))

    reading = json.loads(capsys.readouterr().out)
    assert code == 0
    assert reading["passed"] is True
    assert reading["correlation_id"] == "golden-1"


def test_error_chain_an_unregistered_in_scope_process_fails_the_read_and_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    liveness = dict(LIVENESS)
    liveness["processes"] = [
        *LIVENESS["processes"],
        {"process_id": "stray-process", "host": "example-host", "state": "active"},
    ]
    install_node_overlay(monkeypatch, tmp_path, "node_oneoff_register_compute", POLICY)
    install_node_overlay(monkeypatch, tmp_path, "automation_liveness", liveness)
    code = main([], environ=roots(tmp_path))

    captured = capsys.readouterr()
    assert code == 1
    reading = json.loads(captured.out)
    assert reading["passed"] is False
    assert "stray-process" in reading["unregistered_processes"]


def test_a_missing_overlay_is_refused_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([], environ=roots(tmp_path / "empty"))

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    assert "oneoff-register: refused:" in captured.err
