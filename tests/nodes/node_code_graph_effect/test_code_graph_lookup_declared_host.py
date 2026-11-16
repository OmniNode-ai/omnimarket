# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup from a lab host whose overlays clone is not under the lane's overlay root (OMN-24916).

A lane's ``ONEX_SKILL_OVERLAY_ROOTS`` named ``$OMNI_HOME/overlays/overlays`` on a host whose overlays
clone the lab host table declares beside the registry root, so the lookup found no overlay (exit 3).
``--host`` reads the overlay from the clone the table declares; the LOOKUP line carries the answered
commit's time and the read time; ``--retention-versions`` is the caller's lookup block.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    make_clone,
)

pytestmark = pytest.mark.unit

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lookup: bool = True
) -> Path:
    """A registry root whose overlays clone sits beside it, declared for host h9 only."""
    omni_root = tmp_path / "registry_root"
    omni_root.mkdir()
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not lookup:
        del document["lookup"]
    overlays = tmp_path / "overlays" / "overlays"
    target = overlays / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    table = {
        "hosts": [
            {
                "name": "h9",
                "clones": [
                    {
                        "repo": "OmniNode-ai/overlays",
                        "ref": "main",
                        "path": "$OMNI_HOME/../overlays",
                    }
                ],
            }
        ]
    }
    (overlays / "lab_hosts").mkdir()
    (overlays / "lab_hosts" / "overlay.yaml").write_text(
        yaml.safe_dump(table), encoding="utf-8"
    )
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(omni_root / "overlays" / "overlays")
    )
    monkeypatch.setenv("OMNI_HOME", str(omni_root))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    runtime_code_graph._VERSIONS.clear()
    return omni_root


def _importers(
    capsys: pytest.CaptureFixture[str], omni_root: Path, *extra: str
) -> tuple[int, str, str]:
    code = main(
        [
            "lookup",
            "--question",
            "importers",
            "--repo",
            "example_core",
            "--path",
            "src/example_core/models.py",
            "--workspace-root",
            str(omni_root),
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_lane_overlay_root_alone_finds_no_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, _, err = _importers(capsys, omni_root)
    assert code == 3
    assert "the overlay file is not configured" in err


def test_the_declared_host_clone_answers_with_rows_and_their_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, out, err = _importers(capsys, omni_root, "--host", "h9")
    assert code == 0, out + err
    assert "MATCH src/example_core/cli.py\n" in out
    summary = out.splitlines()[-1]
    assert "status=found" in summary
    assert re.search(rf" committed_at={STAMP} read_at={STAMP} ", summary), summary


def test_a_host_the_table_does_not_declare_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, out, err = _importers(capsys, omni_root, "--host", "h8")
    assert code == 3, out
    assert "h8: the lab host table declares no row for this host" in err


def test_the_caller_lookup_block_answers_where_the_overlay_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, lookup=False)
    code, out, _ = _importers(capsys, omni_root, "--host", "h9")
    assert code == 1
    assert "reason=the overlay has no lookup block" in out
    code, out, err = _importers(
        capsys, omni_root, "--host", "h9", "--retention-versions", "3"
    )
    assert code == 0, out + err
    assert "status=found" in out.splitlines()[-1]
