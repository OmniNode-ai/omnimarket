# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre_commit_store age class: the store's row goes before the folder does.

The store is the pre-commit hook repository index (a sqlite file beside the hook folders it
tracks). A folder that ages out is forgotten in the index under the store's lock before it is
removed, so pre-commit never finds a row whose folder is gone. The store's ``forget`` returns
``forgotten`` when the row was deleted, ``unreferenced`` when there was no row, and ``locked``
when another process holds the store's lock; a folder the store refuses to forget is kept and
the refusal is reported. A folder a live process names is kept before the store is asked.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_retention_effect import runtime_host_retention
from omnimarket.nodes.node_host_retention_effect.handlers import host_retention_ports
from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    SqlitePreCommitStore,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
)
from omnimarket.nodes.node_host_retention_effect.node import NodeHostRetentionEffect
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    NOW,
    FakePreCommitStore,
    age,
    entry,
    make_ports,
    overlay_file,
    precommit_class,
    run,
)

pytestmark = pytest.mark.unit


def store_root(tmp_path: Path) -> Path:
    """The store: an aged repo folder, a young one, and the plain files pre-commit keeps."""
    root = tmp_path / "pre-commit"
    root.mkdir()
    entry(root, "repoaged", days=30)
    entry(root, "reponew", days=1)
    (root / "db.db").write_text("sqlite")
    (root / "README").write_text("the hook repositories it tracks")
    age(root / "db.db", 30)
    age(root / "README", 30)
    return root


def store_db(root: Path, *paths: str) -> str:
    """The store's index beside the hook folders, holding one row per path."""
    db = root / "db.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "CREATE TABLE repos (repo TEXT NOT NULL, ref TEXT NOT NULL, "
            "path TEXT NOT NULL, PRIMARY KEY (repo, ref))"
        )
        for path in paths:
            connection.execute(
                "INSERT INTO repos (repo, ref, path) VALUES (?, ?, ?)",
                (Path(path).name, "main", path),
            )
        connection.commit()
    finally:
        connection.close()
    return str(db)


def test_aged_repo_folder_is_removed_and_its_row_forgotten(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    store = FakePreCommitStore()

    result = run(
        tmp_path,
        {"age_classes": [precommit_class(root)]},
        ports=make_ports(pre_commit=store),
    )

    assert result.status == "ok"
    assert not (root / "repoaged").exists()
    assert (root / "reponew").exists()
    assert (root / "db.db").exists()
    assert (root / "README").exists()
    assert store.calls == [(str(root / "db.db"), str(root / "repoaged"))]
    assert [Path(i.path).name for i in result.items] == ["repoaged"]
    assert {Path(k.path).name: k.reason for k in result.kept} == {
        "db.db": "protected",
        "README": "protected",
        "reponew": "too_new",
    }


def test_dry_run_forgets_and_removes_nothing(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    store = FakePreCommitStore()

    result = run(
        tmp_path,
        {"age_classes": [precommit_class(root)]},
        ports=make_ports(pre_commit=store),
        dry_run=True,
    )

    assert (root / "repoaged").exists()
    assert (root / "reponew").exists()
    assert (root / "db.db").exists()
    assert (root / "README").exists()
    assert store.calls == []
    assert [Path(i.path).name for i in result.items] == ["repoaged"]


def test_a_locked_store_keeps_the_folder(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    store = FakePreCommitStore(result="locked")

    result = run(
        tmp_path,
        {"age_classes": [precommit_class(root)]},
        ports=make_ports(pre_commit=store),
    )

    assert (root / "repoaged").exists()
    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["repoaged"] == "store_locked"
    assert result.errors == ()


def test_a_failed_index_write_keeps_the_folder_and_reports_it(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    store = FakePreCommitStore(fail=True)

    result = run(
        tmp_path,
        {"age_classes": [precommit_class(root)]},
        ports=make_ports(pre_commit=store),
    )

    assert (root / "repoaged").exists()
    assert len(result.errors) == 1
    assert "repoaged" in result.errors[0]
    assert "OSError" in result.errors[0]


def test_a_folder_a_live_process_names_is_kept(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    store = FakePreCommitStore()
    refs = ProcessReferences(
        blobs=[f"{root}/repoaged/py_env-python3.12/bin/python run"]
    )

    result = run(
        tmp_path,
        {"age_classes": [precommit_class(root)]},
        ports=make_ports(pre_commit=store),
        refs=refs,
    )

    assert (root / "repoaged").exists()
    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["repoaged"] == "held_by_process"
    assert store.calls == []


def test_declared_without_index_db_is_refused(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    declared = precommit_class(root)
    del declared["index_db"]

    result = run(tmp_path, {"age_classes": [declared]})

    # The overlay model refuses it; the overlay reader reports the field it rejected, never
    # the pydantic message, so the refusal names the age class a ValidationError struck.
    assert result.status == "refused"
    assert any("age_classes" in r and "ValidationError" in r for r in result.refusals)
    assert (root / "repoaged").exists()


def test_index_db_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    declared = precommit_class(root, index_db=str(tmp_path / "elsewhere" / "db.db"))

    result = run(tmp_path, {"age_classes": [declared]})

    assert result.status == "refused"
    assert any("must sit directly inside the root" in r for r in result.refusals)
    assert (root / "repoaged").exists()


def test_the_real_sqlite_store_deletes_only_the_removed_folders_row(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    db = store_db(root, str(root / "repoaged"), str(root / "reponew"))
    store = SqlitePreCommitStore()

    assert store.forget(db, str(root / "repoaged")) == "forgotten"

    connection = sqlite3.connect(db)
    try:
        rows = connection.execute("SELECT repo FROM repos").fetchall()
    finally:
        connection.close()
    assert rows == [("reponew",)]
    assert store.forget(db, str(root / "repoaged")) == "unreferenced"
    with pytest.raises(OSError, match="index_db absent"):
        store.forget(str(root / "absent.db"), str(root / "repoaged"))


def test_the_real_sqlite_store_reports_a_held_lock(tmp_path: Path) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    db = store_db(root, str(root / "repoaged"))
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

        assert SqlitePreCommitStore().forget(db, str(root / "repoaged")) == "locked"

        connection = sqlite3.connect(db)
        try:
            rows = connection.execute("SELECT repo FROM repos").fetchall()
        finally:
            connection.close()
        assert rows == [("repoaged",)]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_portable_references_read_ps_when_proc_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def ps_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "/usr/bin/python /x/repoaged/bin/hook\n\n/bin/zsh\n", ""
        )

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(host_retention_ports, "read_process_references", lambda: None)
    monkeypatch.setattr(host_retention_ports, "_run", ps_run)
    found = host_retention_ports.read_references_portable()

    assert found is not None
    assert found.blobs == ["/usr/bin/python /x/repoaged/bin/hook", "/bin/zsh"]
    assert seen
    assert seen[0][0] == "ps"

    monkeypatch.setattr(host_retention_ports, "_run", failing_run)
    assert host_retention_ports.read_references_portable() is None

    held = ProcessReferences()
    monkeypatch.setattr(host_retention_ports, "read_process_references", lambda: held)
    monkeypatch.setattr(host_retention_ports, "_run", ps_run)
    seen.clear()
    assert host_retention_ports.read_references_portable() is held
    assert seen == []


def test_golden_chain_pre_commit_store_through_the_node_entry_point(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    aged = entry(root, "repoaged", days=30)
    young = entry(root, "reponew", days=1)
    db = store_db(root, str(aged), str(young))
    node = NodeHostRetentionEffect(
        now=lambda: NOW, ports=make_ports(pre_commit=SqlitePreCommitStore())
    )

    result = node.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, age_classes=[precommit_class(root)]),
        )
    )

    assert result.status == "ok"
    assert [Path(i.path).name for i in result.items] == ["repoaged"]
    assert not aged.exists()
    assert young.exists()
    connection = sqlite3.connect(db)
    try:
        rows = connection.execute("SELECT repo FROM repos").fetchall()
    finally:
        connection.close()
    assert rows == [("reponew",)]
    assert {t.class_name: t for t in result.totals}["pc-store"].removed_count == 1


def test_error_chain_pre_commit_store_without_index_db_ends_refused(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    aged = entry(root, "repoaged", days=30)
    declared = precommit_class(root)
    del declared["index_db"]
    node = NodeHostRetentionEffect(
        now=lambda: NOW, ports=make_ports(pre_commit=SqlitePreCommitStore())
    )

    result = node.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, age_classes=[declared]),
        )
    )

    assert result.status == "refused"
    assert any("age_classes" in r and "ValidationError" in r for r in result.refusals)
    assert aged.exists()


def test_command_line_run_of_the_pre_commit_store_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "pre-commit"
    root.mkdir()
    aged = entry(root, "repoaged", days=30)
    young = entry(root, "reponew", days=1)
    store_db(root, str(aged), str(young))
    monkeypatch.setattr(
        runtime_host_retention,
        "HandlerHostRetention",
        lambda: HandlerHostRetention(
            now=lambda: NOW, ports=make_ports(pre_commit=SqlitePreCommitStore())
        ),
    )
    overlay = overlay_file(tmp_path, age_classes=[precommit_class(root)])

    assert (
        runtime_host_retention.main(
            [
                "run",
                "--host",
                "pool-a",
                "--overlay-file",
                overlay,
                "--class",
                "pre_commit_store",
            ]
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    assert [Path(i["path"]).name for i in printed["items"]] == ["repoaged"]
    assert any(row["class_name"] == "pc-store" for row in printed["totals"])
    assert not aged.exists()
    assert young.exists()
