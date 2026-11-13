# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of node_host_retention_effect: each test fails when either side of the seam changes shape.

overlay <-> contract bounds <-> model, runner state (``docker top``), lane receipts and run directories, unit files.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    CONTRACT_PATH,
    load_bounds,
    validate_declarations,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    DockerCiHostState,
    DockerContainerMounts,
    FilesystemLaneLiveness,
    FilesystemUnitFiles,
    SqlitePreCommitStore,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionOverlay,
    ModelRetentionBounds,
)

pytestmark = pytest.mark.unit

NODE_DIR = CONTRACT_PATH.parent
IDLE = (
    "UID PID PPID C STIME TTY TIME CMD\n"
    "root 55032 54994 0 Nov12 ? 00:00:00 bash /opt/onex-runner-hooks/entrypoint.sh\n"
    "root 55228 55212 0 Nov12 ? 00:00:12 actions-runner/bin/Runner.Listener run\n"
)
BUSY = (
    IDLE
    + "root 2769000 55228 9 00:01 ? 00:00:03 actions-runner/bin/Runner.Worker spawnclient 123 124\n"
)


def completed(monkeypatch: pytest.MonkeyPatch, code: int, out: str) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_overlay_seam_the_example_overlay_loads_and_every_declaration_is_inside_the_contract_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row written ~/... expands where it loads; pin the home so the path check reads a real folder, not a host's symlinked home.
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    overlay = ModelHostRetentionOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )
    bounds = load_bounds()

    assert {row.name for row in overlay.hosts} >= {"pool-a"}
    text = (NODE_DIR / "overlay.example.yaml").read_text()
    assert "/home/" not in text  # a home path is written as ~/ and expanded at load
    for row in overlay.hosts:
        assert validate_declarations(row, bounds) == [], row.name
        for age in row.age_classes:
            assert age.kind in bounds.root_leaf_names


def test_contract_seam_names_resolve_and_topics_agree() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    for block in (
        contract["input_model"],
        contract["output_model"],
        contract["handler"],
        contract["entrypoint"],
    ):
        module = importlib.import_module(block["module"])
        assert hasattr(module, block["name"])
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["topic"] == contract["runtime_dispatch"]["command_topic"]
    assert (
        contract["terminal_event"]
        == contract["runtime_dispatch"]["terminal_events"]["success"]
    )
    for topic in contract["runtime_dispatch"]["terminal_events"].values():
        assert topic in contract["externally_consumed_topics"]
    assert ModelRetentionBounds.model_validate(contract["retention_bounds"])
    module_path, _, attribute = contract["overlay"]["model"].rpartition(".")
    assert (
        getattr(importlib.import_module(module_path), attribute)
        is ModelHostRetentionOverlay
    )
    assert (
        contract["name"]
        in yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())["entry_points"][
            "onex.nodes"
        ]
    )


def test_the_node_is_registered_in_the_package_entry_points() -> None:
    pyproject = NODE_DIR.parents[3] / "pyproject.toml"
    if not pyproject.exists():  # an installed copy carries no project file
        pytest.fail("pyproject.toml not found beside the source tree")
    assert (
        'node_host_retention_effect = "omnimarket.nodes.node_host_retention_effect"'
        in pyproject.read_text()
    )


def test_runner_state_seam_reads_a_recorded_docker_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = completed(monkeypatch, 0, IDLE)
    assert DockerCiHostState(5).is_idle("ci-runner-1") is True
    assert seen == [["docker", "top", "ci-runner-1", "-eo", "pid,args"]]
    completed(monkeypatch, 0, BUSY)
    assert DockerCiHostState(5).is_idle("ci-runner-1") is False
    completed(monkeypatch, 1, "")
    assert DockerCiHostState(5).is_idle("ci-runner-1") is None


def test_container_mounts_seam_reads_recorded_docker_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(
        [(0, "abc123\ndef456\n"), (0, "/data/a\n\n/data/b/c\nvolume-name\n")]
    )

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        code, out = next(answers)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert DockerContainerMounts(5).mount_sources() == {"/data/a", "/data/b/c"}
    completed(monkeypatch, 1, "")
    assert DockerContainerMounts(5).mount_sources() is None


def test_receipt_seam_lane_run_directories_and_receipts(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    live = runs / "rlane-demo-lane-0a1b2c.ctx"
    live.mkdir(parents=True)
    (live / "run.pid").write_text(f"{os.getpid()}\n")
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    dead = runs / "rlane-dead-lane-ffeedd.ctx"
    dead.mkdir()
    (dead / "run.pid").write_text(f"{finished.pid}\n")
    (runs / "unrelated").mkdir()
    receipts = tmp_path / "receipts"
    (receipts / "lane-x").mkdir(parents=True)
    (receipts / "lane-x" / "r.json").write_text('{"final": false}')
    (receipts / "lane-y").mkdir()
    (receipts / "lane-y" / "r.json").write_text('{"final": true}')
    (receipts / "lane-z").mkdir()
    (receipts / "lane-z" / "r.json").write_text("not json")

    lanes = FilesystemLaneLiveness().live_lanes(
        [str(runs), str(tmp_path / "missing")], [str(receipts)]
    )

    assert lanes == {"demo-lane", "lane-x"}
    assert FilesystemLaneLiveness().live_lanes([str(tmp_path / "missing")], []) == set()


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any directory")
def test_receipt_seam_an_unreadable_root_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0)
    try:
        assert FilesystemLaneLiveness().live_lanes([str(runs)], []) is None
    finally:
        runs.chmod(0o700)


def test_unit_files_seam_reads_units_and_drop_ins_only(tmp_path: Path) -> None:
    (tmp_path / "a.service").write_text("ExecStart=/x/one")
    (tmp_path / "a.service.d").mkdir()
    (tmp_path / "a.service.d" / "o.conf").write_text("Environment=P=/x/two")
    (tmp_path / "notes.txt").write_text("/x/three")

    texts = FilesystemUnitFiles().unit_texts([str(tmp_path), str(tmp_path / "missing")])

    assert texts is not None
    assert sorted(texts) == ["Environment=P=/x/two", "ExecStart=/x/one"]


def test_pre_commit_seam_h101_style_mac_row_declares_the_store_the_tool_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    overlay = ModelHostRetentionOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )
    mac = next(row for row in overlay.hosts if row.name == "pool-mac")

    assert validate_declarations(mac, load_bounds()) == []
    store = next(age for age in mac.age_classes if age.kind == "pre_commit_store")
    assert Path(store.root).name == "pre-commit"
    assert store.index_db is not None
    assert Path(store.index_db).name == "db.db"
    assert Path(store.index_db).parent == Path(store.root)
    assert [Path(cache.path).name for cache in mac.uv_caches] == ["uv"]


def test_pre_commit_seam_the_index_schema_the_port_writes_matches_the_tool(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    db = root / "db.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE repos (repo TEXT NOT NULL, ref TEXT NOT NULL, "
        "path TEXT NOT NULL, PRIMARY KEY (repo, ref))"
    )
    connection.execute(
        "INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)",
        (
            "https://github.com/pre-commit/pre-commit-hooks",
            "v5.0.0",
            str(root / "repoaged"),
        ),
    )
    connection.execute(
        "INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)",
        ("https://github.com/pre-commit/mirrors-mypy", "v4.6.0", str(root / "reponew")),
    )
    connection.execute("CREATE TABLE configs (path TEXT NOT NULL, PRIMARY KEY (path))")
    connection.execute(
        "INSERT INTO configs (path) VALUES (?)",
        (str(root / "repoaged" / ".pre-commit-config.yaml"),),
    )
    connection.commit()
    connection.close()

    assert SqlitePreCommitStore().forget(str(db), str(root / "repoaged")) == "forgotten"

    check = sqlite3.connect(db)
    try:
        assert [row[0] for row in check.execute("SELECT path FROM repos")] == [
            str(root / "reponew")
        ]
        assert [row[0] for row in check.execute("SELECT path FROM configs")] == [
            str(root / "repoaged" / ".pre-commit-config.yaml")
        ]
    finally:
        check.close()


def test_pre_commit_seam_the_contract_bounds_name_the_leaf_folders() -> None:
    bounds = load_bounds()

    assert bounds.root_leaf_names["pre_commit_store"] == ("pre-commit",)
    assert "uv" in bounds.root_leaf_names["uv_cache"]
