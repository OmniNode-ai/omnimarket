# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``added-nodes`` and ``list-missing`` commands of the overlay main load check node (OMN-26490), run as CI runs them."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_main_load_check_effect.runtime_overlay_main_load_check import (
    main,
)

NODE = "node_new_reader_effect"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _repo_adding_a_node_that_reads_an_overlay(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    nodes = repo / "src/omnimarket/nodes"
    (nodes / "node_base_compute").mkdir(parents=True)
    (nodes / "node_base_compute/contract.yaml").write_text(
        "name: base\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (nodes / NODE).mkdir()
    (nodes / NODE / "contract.yaml").write_text("name: new\n", encoding="utf-8")
    (nodes / NODE / "overlay.example.yaml").write_text("key: value\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "OMN-1: add the node (#5)")
    return repo


def _overlays(tmp_path: Path, *, with_document: bool) -> Path:
    root = tmp_path / "overlays-main"
    (root / "overlays").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    if with_document:
        (root / "overlays" / NODE).mkdir()
        (root / "overlays" / NODE / "overlay.yaml").write_text(
            "key: value\n", encoding="utf-8"
        )
    (root / "overlays" / "node_unrelated").mkdir()
    (root / "overlays" / "node_unrelated" / "overlay.yaml").write_text(
        "k: v\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (#1)")
    return root


def _added_nodes(repo: Path, overlays: Path) -> list[str]:
    return [
        "added-nodes",
        "--repo-root",
        str(repo),
        "--base-ref",
        "HEAD^1",
        "--overlays-root",
        str(overlays / "overlays"),
        "--overlays-checkout",
        str(overlays),
    ]


def test_added_node_without_its_overlay_fails_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "adds a node")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)

    code = main(_added_nodes(repo, _overlays(tmp_path, with_document=False)))

    out = capsys.readouterr().out
    assert code == 1
    assert f"refused {NODE}" in out
    assert f"overlays/{NODE}/overlay.yaml" in out
    assert "overlay-required: added=1 refused=1" in out


def test_added_node_with_its_overlay_passes_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)

    code = main(_added_nodes(repo, _overlays(tmp_path, with_document=True)))

    assert code == 0
    assert "overlay-required: added=1 refused=0" in capsys.readouterr().out


def test_a_node_that_was_already_on_the_base_is_not_judged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)
    # the head becomes the base: nothing is added against it, though the node still has no overlay
    args = _added_nodes(repo, _overlays(tmp_path, with_document=False))
    args[args.index("HEAD^1")] = "HEAD"

    code = main(args)

    assert code == 0
    assert "overlay-required: added=0 refused=0" in capsys.readouterr().out


def test_list_missing_names_the_node_overlay_and_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)

    code = main(
        [
            "list-missing",
            "--repo-root",
            str(repo),
            "--overlays-root",
            str(_overlays(tmp_path, with_document=False) / "overlays"),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"{NODE} | {NODE} | unowned (OMN-1)" in out
    assert "overlay-required: missing=1" in out


def test_added_node_whose_overlay_is_in_the_store_snapshot_passes_and_reports_the_store_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)
    snapshot = tmp_path / "store-snapshot"
    now = datetime(2026, 11, 23, tzinfo=UTC)
    FileOverlaySnapshot(snapshot).write(
        ModelOverlayStoreRecord(
            overlay_id=NODE,
            digest=document_digest({"key": "value"}),
            document={"key": "value"},
            source_ref="overlays@main",
            loaded_by="test",
            loaded_at=now,
        ),
        written_at=now,
    )

    code = main(
        [
            *_added_nodes(repo, _overlays(tmp_path, with_document=False)),
            "--store-snapshot-root",
            str(snapshot),
        ]
    )

    assert code == 0
    assert "overlay-required: added=1 refused=0 store=read" in capsys.readouterr().out


def test_a_store_snapshot_root_that_is_absent_is_indeterminate_never_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)

    code = main(
        [
            *_added_nodes(repo, _overlays(tmp_path, with_document=True)),
            "--store-snapshot-root",
            str(tmp_path / "no-such-root"),
        ]
    )

    assert code == 3
    assert "is not a directory" in capsys.readouterr().err


def test_a_refusal_without_a_store_snapshot_reports_the_store_unread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", "")
    repo = _repo_adding_a_node_that_reads_an_overlay(tmp_path)

    code = main(_added_nodes(repo, _overlays(tmp_path, with_document=False)))

    out = capsys.readouterr().out
    assert code == 1
    assert "the settings store was not read" in out
    assert "store=unread" in out
