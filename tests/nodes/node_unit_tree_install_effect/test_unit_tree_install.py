# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_unit_tree_install_effect installs a tree from one commit, keeps the preserved paths, verifies it and rolls back on a failed check."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_unit_tree_install_effect.handlers.handler_unit_tree_install import (
    HandlerUnitTreeInstall,
)
from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    FAIL,
    PASS,
    commit_all,
    environ,
    git,
    make_overlay,
    make_root,
    request,
)


def _handler(
    root: Path,
    smoke: tuple[tuple[str, ...], ...] = (PASS,),
) -> HandlerUnitTreeInstall:
    return HandlerUnitTreeInstall(make_overlay(smoke=smoke), environ(root))


def test_install_builds_the_commit_keeps_the_venv_and_records_the_revision(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    tree = root / "units" / "u1" / "tree"
    (tree / ".venv").mkdir(parents=True)
    (tree / ".venv" / "marker").write_text("keep\n", encoding="utf-8")
    (tree / "stale.txt").write_text("old\n", encoding="utf-8")
    tip = git(root / "clone", "rev-parse", "HEAD")
    result = _handler(root).handle(request())
    assert result.verdict == "pass", result
    assert result.commit == tip
    assert (tree / "a.txt").read_text(encoding="utf-8") == "a1\n"
    assert not (tree / "stale.txt").exists()
    assert (tree / ".venv" / "marker").read_text(encoding="utf-8") == "keep\n"
    assert result.kept.startswith("tree.rollback-")
    assert f'"commit": "{tip}"' in (root / "units" / "u1" / "record.json").read_text(
        encoding="utf-8"
    )


def test_a_second_commit_replaces_the_tree_and_verify_reads_the_record(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    handler = _handler(root)
    assert handler.handle(request()).verdict == "pass"
    clone = root / "clone"
    (clone / "a.txt").write_text("a2\n", encoding="utf-8")
    (clone / "b.txt").unlink()
    (clone / "c.txt").write_text("c1\n", encoding="utf-8")
    tip = commit_all(clone, "two")
    assert handler.handle(request()).commit == tip
    tree = root / "units" / "u1" / "tree"
    assert (tree / "a.txt").read_text(encoding="utf-8") == "a2\n"
    assert not (tree / "b.txt").exists()
    assert (tree / "c.txt").exists()
    verified = handler.handle(request("verify"))
    assert verified.verdict == "pass"
    assert verified.commit == tip
    (tree / "a.txt").write_text("hand edit\n", encoding="utf-8")
    assert handler.handle(request("verify")).verdict == "fail"


def test_a_unit_or_host_the_overlay_does_not_name_is_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    handler = _handler(root)
    assert handler.handle(request(unit="other")).verdict == "refused"
    wrong = handler.handle(request(host="h2"))
    assert wrong.verdict == "refused"
    assert [c.name for c in wrong.checks if not c.ok] == ["host"]
    assert not (root / "units").exists()


def test_verify_without_a_record_fails_on_the_recorded_revision(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    result = _handler(root).handle(request("verify"))
    assert result.verdict == "fail"
    assert [c.name for c in result.checks if not c.ok] == ["recorded-revision"]


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    result = _handler(root).handle(request("dry-run"))
    assert result.verdict == "dry-run"
    assert not (root / "units" / "u1" / "tree").exists()
    assert not (root / "units" / "u1" / "record.json").exists()


def test_a_failing_smoke_puts_the_previous_tree_back_and_a_passing_one_does_not(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    assert _handler(root, smoke=(PASS,)).handle(request()).verdict == "pass"
    clone = root / "clone"
    (clone / "a.txt").write_text("a2\n", encoding="utf-8")
    commit_all(clone, "two")
    tree = root / "units" / "u1" / "tree"
    (tree / ".venv").mkdir()
    (tree / ".venv" / "marker").write_text("keep\n", encoding="utf-8")
    result = _handler(root, smoke=(PASS, FAIL)).handle(request())
    assert result.verdict == "rolled-back"
    assert [c.name for c in result.checks if not c.ok] == ["smoke 2"]
    assert (tree / "a.txt").read_text(encoding="utf-8") == "a1\n"
    assert (tree / ".venv" / "marker").exists()
    assert list(tree.parent.glob("tree.failed-*"))
    assert _handler(root, smoke=(PASS, PASS)).handle(request()).verdict == "pass"
    assert (tree / "a.txt").read_text(encoding="utf-8") == "a2\n"
