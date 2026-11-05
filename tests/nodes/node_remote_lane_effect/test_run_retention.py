# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run retention preserves evidence before removing a closed lane's scratch trees."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_remote_lane_effect.run_retention import _FINAL, retain_run


def _git(tree: Path, *args: str) -> str:
    """Exercise real repositories without inheriting the lane's git routing."""
    done = subprocess.run(
        ["git", "-C", str(tree), *args],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return done.stdout


@pytest.fixture
def clone(run, tmp_path):
    root, tree, ctx, receipt, ledger = run
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    (seed / "report.md").write_text("measured result")
    _git(seed, "add", "report.md")
    _git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "seed",
    )
    (tree / "report.md").unlink()
    _git(root, "clone", str(seed), str(tree))
    assert not _git(tree, "status", "--porcelain")
    assert (
        _git(tree, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() == "0"
    )
    return root, tree, ctx, receipt, ledger


@pytest.mark.parametrize(
    "state", ["clean", "modified", "staged", "untracked", "unpushed", "unfinished"]
)
def test_finished_clone_retention_checks_work_and_remote_commits(clone, state):
    root, tree, ctx, receipt, ledger = clone
    if state in {"modified", "staged", "unpushed"}:
        (tree / "report.md").write_text("local work")
    if state in {"staged", "unpushed"}:
        _git(tree, "add", "report.md")
    if state == "unpushed":
        _git(
            tree,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "local",
        )
    if state == "untracked":
        (tree / "draft.txt").write_text("local work")
    if state == "unfinished":
        with pytest.raises(ValueError, match="ownership"):
            retain_run(root, receipt, ledger.splitlines()[0], delete=True)
        assert tree.exists()
        assert ctx.exists()
        return
    result = retain_run(root, receipt, ledger, delete=True)
    assert Path(result["archive"], "receipt.json").is_file()
    if state == "clean":
        assert result["removed"] == ["run-1", "run-1.ctx"]
        assert not tree.exists()
        assert not ctx.exists()
    else:
        assert result["removed"] == []
        reason = "unpushed-commits" if state == "unpushed" else "uncommitted-changes"
        assert reason in result["kept"]["run-1"]
        assert tree.exists()
        assert ctx.exists()


@pytest.mark.parametrize("head", ["detached", "no-upstream"])
def test_remote_reachable_clone_needs_no_tracking_branch(clone, head):
    root, tree, _ctx, receipt, ledger = clone
    if head == "detached":
        _git(tree, "checkout", "--detach", "HEAD")
    else:
        _git(tree, "branch", "--unset-upstream")
    assert retain_run(root, receipt, ledger, delete=True)["removed"] == [
        "run-1",
        "run-1.ctx",
    ]


def test_unpushed_commit_on_another_branch_is_kept(clone):
    root, tree, ctx, receipt, ledger = clone
    _git(tree, "checkout", "-b", "local")
    _git(
        tree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "local",
    )
    _git(tree, "checkout", "main")
    result = retain_run(root, receipt, ledger, delete=True)
    assert "unpushed-commits" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


def test_clone_holding_a_peer_worktree_git_dir_is_kept(clone, tmp_path):
    root, tree, ctx, receipt, ledger = clone
    peer = tmp_path / "peer"
    _git(tree, "worktree", "add", "--detach", str(peer), "HEAD")
    result = retain_run(root, receipt, ledger, delete=True)
    assert "holds-worktree-git-dir" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()
    assert _git(peer, "rev-parse", "HEAD") == _git(tree, "rev-parse", "HEAD")


def test_clean_linked_tree_with_git_metadata_outside_the_run_is_removed(
    clone, tmp_path
):
    root, tree, ctx, receipt, ledger = clone
    primary = tmp_path / "primary"
    tree.rename(primary)
    _git(primary, "worktree", "add", "--detach", str(tree), "HEAD")
    assert (tree / ".git").is_file()
    result = retain_run(root, receipt, ledger, delete=True)
    assert result["removed"] == ["run-1", "run-1.ctx"]
    assert not tree.exists()
    assert not ctx.exists()
    assert _git(primary, "rev-parse", "HEAD")


@pytest.mark.parametrize("state", ["clean", "dirty", "unpushed"])
def test_ignored_nested_clone_is_checked_before_either_tree_is_removed(clone, state):
    root, tree, ctx, receipt, ledger = clone
    nested = tree / "nested"
    _git(root, "clone", str(tree), str(nested))
    (tree / ".git/info/exclude").write_text("nested/\n")
    if state == "dirty":
        (nested / "report.md").write_text("nested work")
    if state == "unpushed":
        _git(
            nested,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "nested",
        )
    assert not _git(tree, "status", "--porcelain")
    result = retain_run(root, receipt, ledger, delete=True)
    if state == "clean":
        assert result["removed"] == ["run-1", "run-1.ctx"]
    else:
        assert result["removed"] == []
        assert "run-1/nested" in result["kept"]["run-1"]
        assert tree.exists()
        assert ctx.exists()


def test_clone_without_remote_refs_is_kept(clone):
    root, tree, ctx, receipt, ledger = clone
    _git(tree, "remote", "remove", "origin")
    result = retain_run(root, receipt, ledger, delete=True)
    assert "unpushed-commits" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


def test_clone_with_hidden_untracked_files_is_kept(clone):
    root, tree, ctx, receipt, ledger = clone
    _git(tree, "config", "status.showUntrackedFiles", "no")
    (tree / "draft.txt").write_text("local work")
    assert not _git(tree, "status", "--porcelain")
    result = retain_run(root, receipt, ledger, delete=True)
    assert "uncommitted-changes" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


def test_context_git_symlink_preserves_both_trees(clone):
    root, tree, ctx, receipt, ledger = clone
    metadata = ctx / "git-common"
    metadata.mkdir()
    (metadata / ".git").symlink_to(tree / ".git", target_is_directory=True)
    result = retain_run(root, receipt, ledger, delete=True)
    assert result["removed"] == []
    assert "symlink" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()
    assert _git(tree, "rev-parse", "HEAD")


def test_unreadable_git_status_preserves_both_trees(clone, monkeypatch):
    from omnimarket.handlers import lab_retention_tree_safety as retention_tree_safety

    root, tree, ctx, receipt, ledger = clone

    def timeout(*args):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(retention_tree_safety, "_git", timeout)
    result = retain_run(root, receipt, ledger, delete=True)
    assert result["removed"] == []
    assert "status-unreadable" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


@pytest.fixture
def run(tmp_path: Path):
    root = tmp_path / "lab-run"
    tree = root / "runs/run-1"
    ctx = root / "runs/run-1.ctx"
    tree.mkdir(parents=True)
    ctx.mkdir()
    (tree / "report.md").write_text("measured result")
    (ctx / "result.txt").write_text("done")
    receipt = {
        "run_id": "run-1",
        "lane": "lane-1",
        "final": True,
        "status": "done",
        "ended_at": "2026-10-24T00:00:00Z",
        "report": str(tree / "report.md"),
        "result": "LANE_RESULT outcome=done",
    }
    ledger = "2026-10-23T00:00:00Z | CLAIM | lane=lane-1 | run=run-1\n2026-10-24T00:00:00Z | TERMINAL | lane=lane-1 | closes-CLAIM=2026-10-23T00:00:00Z\n"
    return root, tree, ctx, receipt, ledger


def test_archives_report_result_receipt_before_deleting(run):
    root, tree, ctx, receipt, ledger = run
    result = retain_run(root, receipt, ledger, delete=True)
    archive = root / "reports/lane-1/run-1"
    assert (archive / "files/runs/run-1/report.md").read_text() == "measured result"
    assert (archive / "files/runs/run-1.ctx/result.txt").read_text() == "done"
    assert json.loads((archive / "receipt.json").read_text())["final"] is True
    assert not tree.exists()
    assert not ctx.exists()
    assert result["removed"] == ["run-1", "run-1.ctx"]


@pytest.mark.parametrize(
    "change",
    [
        "running",
        "not-final",
        "missing-report",
        "live-claim",
        "no-ledger",
        "no-claim",
        "wrong-terminal",
        "unsafe-id",
        "wrong-run",
    ],
)
def test_uncertainty_preserves_both_trees(run, change):
    root, tree, ctx, receipt, ledger = run
    if change == "running":
        receipt["status"] = "running"
    if change == "not-final":
        receipt["final"] = False
    if change == "missing-report":
        receipt["report"] = str(tree / "missing.md")
    if change == "live-claim":
        ledger += "2026-10-25T00:00:00Z | CLAIM | lane=lane-1 | run=run-2\n"
    if change == "no-ledger":
        ledger = ""
    if change == "no-claim":
        ledger = ledger.splitlines()[1]
    if change == "wrong-terminal":
        ledger = ledger.replace("closes-CLAIM=2026-10-23", "closes-CLAIM=2026-10-22")
    if change == "unsafe-id":
        receipt["run_id"] = "../outside"
    if change == "wrong-run":
        receipt["run_id"] = "run-2"
    with pytest.raises((ValueError, OSError)):
        retain_run(root, receipt, ledger, delete=True)
    assert tree.exists()
    assert ctx.exists()


def test_git_tree_is_kept_even_with_final_receipt(run):
    root, tree, ctx, receipt, ledger = run
    (tree / ".git").write_text("gitdir: elsewhere")
    result = retain_run(root, receipt, ledger, delete=True)
    assert tree.exists()
    assert ctx.exists()
    assert result["removed"] == []


def test_symlink_cannot_escape_lab_root(run, tmp_path):
    root, tree, ctx, receipt, ledger = run
    outside = tmp_path / "outside"
    outside.write_text("private")
    (tree / "report.md").unlink()
    (tree / "report.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        retain_run(root, receipt, ledger, delete=True)
    assert outside.read_text() == "private"
    assert tree.exists()
    assert ctx.exists()


def test_archive_only_never_removes_work(run):
    root, tree, ctx, receipt, ledger = run
    assert retain_run(root, receipt, ledger)["removed"] == []
    assert tree.exists()
    assert ctx.exists()


def test_copy_failure_keeps_work_and_prior_reports(run, monkeypatch):
    import shutil

    root, tree, ctx, receipt, ledger = run

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        retain_run(root, receipt, ledger, delete=True)
    assert tree.exists()
    assert ctx.exists()


def test_prunes_only_expired_managed_reports_of_closed_lanes(run):
    root, _tree, _ctx, receipt, ledger = run
    retain_run(root, receipt, ledger, now=1792800000)
    old = root / "reports/lane-1/run-1"
    unmanaged = root / "reports/manual"
    unmanaged.mkdir()
    (unmanaged / "notes").write_text("keep")
    receipt = {**receipt, "run_id": "run-2", "ended_at": "2026-10-25T00:00:00Z"}
    result = retain_run(
        root, receipt, ledger.replace("run-1", "run-2"), keep_days=1, now=1792972801
    )
    assert not old.exists()
    assert unmanaged.exists()
    assert result["pruned"] == ["lane-1/run-1"]


def test_retention_operation_resolves_from_the_packaged_contract(clone):
    import importlib
    from importlib.resources import files

    import yaml

    root, tree, ctx, receipt, ledger = clone
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_remote_lane_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    route = next(
        r
        for r in contract["handler_routing"]["handlers"]
        if r["operation"] == "retain_finished_remote_run"
    )
    module, _, name = route["input_model"].rpartition(".")
    request = getattr(importlib.import_module(module), name)(
        root=str(root), receipt=receipt, ledger=ledger, delete=True
    )
    handler = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )()
    result = handler.handle(request)
    assert result.removed == ["run-1", "run-1.ctx"]
    assert not tree.exists()
    assert not ctx.exists()


@pytest.mark.parametrize("state", ["clean", "dirty", "unpushed", "peer-worktree"])
def test_standalone_host_payload_uses_the_tested_implementation(clone, state, tmp_path):
    import subprocess

    from omnimarket.nodes.node_remote_lane_effect.run_retention import retention_script

    root, tree, ctx, receipt, ledger = clone
    if state == "dirty":
        (tree / "report.md").write_text("local work")
    elif state == "unpushed":
        _git(
            tree,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "local",
        )
    elif state == "peer-worktree":
        _git(tree, "worktree", "add", "--detach", str(tmp_path / "peer"), "HEAD")
    done = subprocess.run(
        ["/bin/sh", "-c", retention_script(str(root), receipt, ledger, delete=True)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("LANE-RETENTION ")
    result = json.loads(done.stdout.removeprefix("LANE-RETENTION "))
    assert Path(result["archive"], "receipt.json").is_file()
    if state == "clean":
        assert result["removed"] == ["run-1", "run-1.ctx"]
        assert not tree.exists()
        assert not ctx.exists()
    else:
        assert result["removed"] == []
        assert result["kept"]["run-1"]
        assert tree.exists()
        assert ctx.exists()


def test_expiration_uses_final_archive_receipt_after_ledger_rollover(run):
    root, _tree, _ctx, receipt, ledger = run
    retain_run(root, receipt, ledger, now=1792800000)
    next_receipt = {
        **receipt,
        "run_id": "run-2",
        "lane": "lane-2",
        "ended_at": "2026-10-25T00:00:00Z",
    }
    next_ledger = ledger.replace("lane-1", "lane-2").replace("run-1", "run-2")
    result = retain_run(root, next_receipt, next_ledger, keep_days=1, now=1792972801)
    assert result["pruned"] == ["lane-1/run-1"]
    assert not (root / "reports/lane-1/run-1").exists()


def test_dispatch_cli_replaces_recorded_launcher_import_failure(run, tmp_path):
    """The launcher cannot import Market; its dispatch process must build the payload."""
    import os
    import subprocess
    import sys

    recorded = "REMOTE-LANE-RETENTION-KEPT lane=m4-board-read-mac-2342: No module named 'omnimarket'"
    bare = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "import omnimarket"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bare.returncode == 1
    assert recorded.partition(": ")[2] in bare.stderr
    root, tree, ctx, receipt, ledger = run
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"root": str(root), "receipt": receipt, "ledger": ledger, "delete": True}
        )
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")}
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_remote_lane_effect.run_retention",
            "--script",
            str(request),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert built.returncode == 0, built.stderr
    assert built.stdout.startswith("python3 - <<'LANE_RETENTION_PY'\n"), (
        recorded,
        built.stdout,
    )
    # Building the payload has no cleanup effect.
    assert tree.exists()
    assert ctx.exists()
    archived = subprocess.run(
        ["/bin/sh", "-c", built.stdout], capture_output=True, text=True, timeout=30
    )
    assert archived.returncode == 0, archived.stderr
    assert archived.stdout.startswith("LANE-RETENTION ")
    assert not tree.exists()
    assert not ctx.exists()


@pytest.mark.parametrize(
    "bad", [{"keep_days": True}, {"delete": "yes"}, {"unexpected": 1}]
)
def test_dispatch_cli_rejects_invalid_request_before_emitting_payload(
    run, tmp_path, bad
):
    import os
    import subprocess
    import sys

    root, tree, ctx, receipt, ledger = run
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"root": str(root), "receipt": receipt, "ledger": ledger, **bad})
    )
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_remote_lane_effect.run_retention",
            "--script",
            str(request),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode != 0
    assert not done.stdout
    assert tree.exists()
    assert ctx.exists()


@pytest.fixture
def symlinked(tmp_path: Path):
    """The recorded host layout: $HOME/lab-run is a symlink to /data/lab-run.

    One ended lane (final receipt, CLAIM closed by a TERMINAL) and one live lane
    (CLAIM only) share the real root. The launcher addresses both through the link.
    """
    real = tmp_path / "data/lab-run"
    link = tmp_path / "home/lab-run"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    trees: dict[str, tuple[Path, Path]] = {}
    for run in ("rlane-ended-aaa111", "rlane-live-bbb222"):
        tree, ctx = real / "runs" / run, real / "runs" / (run + ".ctx")
        tree.mkdir(parents=True)
        ctx.mkdir()
        (tree / "work.txt").write_text("scratch")
        (ctx / "result.txt").write_text("done")
        trees[run] = (tree, ctx)
    receipt = {
        "run_id": "rlane-ended-aaa111",
        "lane": "ended",
        "final": True,
        "status": "done",
        "ended_at": "2026-11-02T01:00:00Z",
        "report": str(link / "runs/rlane-ended-aaa111.ctx/result.txt"),
        "result": "LANE_RESULT outcome=done",
    }
    ledger = (
        "2026-11-02T00:00:00Z | CLAIM | lane=ended | run=rlane-ended-aaa111\n"
        "2026-11-02T00:30:00Z | CLAIM | lane=live | run=rlane-live-bbb222\n"
        "2026-11-02T01:00:00Z | TERMINAL | lane=ended | closes-CLAIM=2026-11-02T00:00:00Z\n"
    )
    return real, link, trees, receipt, ledger


def test_symlinked_lab_root_removes_ended_lane_and_keeps_live_lane(symlinked):
    real, link, trees, receipt, ledger = symlinked
    result = retain_run(link, receipt, ledger, delete=True)
    ended_tree, ended_ctx = trees["rlane-ended-aaa111"]
    live_tree, live_ctx = trees["rlane-live-bbb222"]
    assert result["removed"] == ["rlane-ended-aaa111", "rlane-ended-aaa111.ctx"]
    assert not ended_tree.exists()
    assert not ended_ctx.exists()
    assert (live_tree / "work.txt").read_text() == "scratch"
    assert (live_ctx / "result.txt").read_text() == "done"
    archive = real / "reports/ended/rlane-ended-aaa111"
    assert result["archive"] == str(archive)
    assert (
        archive / "files/runs/rlane-ended-aaa111.ctx/result.txt"
    ).read_text() == "done"


def test_symlinked_lab_root_still_refuses_a_live_lane(symlinked):
    _real, link, trees, receipt, ledger = symlinked
    live = {**receipt, "run_id": "rlane-live-bbb222", "lane": "live"}
    live.pop("report")
    with pytest.raises(ValueError, match="ownership"):
        retain_run(link, live, ledger, delete=True)
    for tree, ctx in trees.values():
        assert tree.exists()
        assert ctx.exists()


def test_symlinked_lab_root_standalone_payload(symlinked):
    import subprocess

    from omnimarket.nodes.node_remote_lane_effect.run_retention import retention_script

    _real, link, trees, receipt, ledger = symlinked
    done = subprocess.run(
        ["/bin/sh", "-c", retention_script(str(link), receipt, ledger, delete=True)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("LANE-RETENTION ")
    assert not trees["rlane-ended-aaa111"][0].exists()
    assert trees["rlane-live-bbb222"][0].exists()


def test_symlink_inside_the_tree_is_still_refused_through_a_symlinked_root(
    symlinked, tmp_path
):
    _real, link, trees, receipt, ledger = symlinked
    outside = tmp_path / "outside"
    outside.write_text("private")
    ctx = trees["rlane-ended-aaa111"][1]
    (ctx / "result.txt").unlink()
    (ctx / "result.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        retain_run(link, receipt, ledger, delete=True)
    assert outside.read_text() == "private"
    assert trees["rlane-ended-aaa111"][0].exists()


def test_symlinked_run_directory_inside_the_root_is_still_refused(symlinked, tmp_path):
    _real, link, trees, receipt, ledger = symlinked
    tree, ctx = trees["rlane-ended-aaa111"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("keep")
    ctx.rename(elsewhere / "ctx")
    ctx.symlink_to(elsewhere / "ctx")
    with pytest.raises(ValueError, match="symlink"):
        retain_run(link, receipt, ledger, delete=True)
    assert (elsewhere / "ctx/result.txt").exists()
    assert tree.exists()


@pytest.mark.parametrize("target", ["data/other", "data/missing"])
def test_symlink_to_a_directory_not_named_lab_run_is_refused(
    symlinked, tmp_path, target
):
    _real, link, trees, receipt, ledger = symlinked
    link.unlink()
    destination = tmp_path / target
    if target.endswith("other"):
        (destination / "runs").mkdir(parents=True)
    link.symlink_to(destination)
    with pytest.raises(ValueError, match="lab-run"):
        retain_run(link, receipt, ledger, delete=True)
    assert all(t.exists() and c.exists() for t, c in trees.values())


def test_symlinked_parent_of_the_lab_root_resolves(symlinked, tmp_path):
    real, _link, trees, receipt, ledger = symlinked
    parent_link = tmp_path / "mnt"
    parent_link.symlink_to(real.parent)
    receipt = {
        **receipt,
        "report": str(parent_link / "lab-run/runs/rlane-ended-aaa111.ctx/result.txt"),
    }
    result = retain_run(parent_link / "lab-run", receipt, ledger, delete=True)
    assert result["removed"] == ["rlane-ended-aaa111", "rlane-ended-aaa111.ctx"]
    assert trees["rlane-live-bbb222"][0].exists()


# Every receipt status onex_remote_lane.py can write for a lane that was placed.
# True: reaches retention (the engine ran or was set up, the run is over, its trees
# are the lane's own). False: kept, with the reason the closeout cannot or must not
# retain it. A status the launcher writes before the engine starts never reaches
# retain_remote at all (it returns when engine_started is false), so these are kept
# for that reason and not judged by _FINAL.
_STATUS_TABLE: dict[str, tuple[bool, str]] = {
    "done": (True, "the engine ended with a result"),
    "failed": (True, "the engine ended with an unstarted-lane failure"),
    "lane-error": (True, "the engine ended with an error result"),
    "no-result": (True, "the engine ended with no result event"),
    "lane-timeout": (True, "the deadline ended the engine"),
    "crashed": (True, "the launcher caught an exit; the engine is not running"),
    "remote-failed": (True, "the remote step failed; no engine is running"),
    "interrupted": (True, "the launcher was interrupted; the guard ends the engine"),
    "timeout": (True, "the run timed out"),
    "host-limited": (True, "the host refused the lane for its limit"),
    "rejected-no-delegation": (
        True,
        "closed by the launcher without a delegation line",
    ),
    "guard-runtime-max": (True, "the lane guard ended the engine at its runtime limit"),
    "guard-memory-max": (True, "the lane guard ended the engine at its memory limit"),
    "glm-usage-limit": (True, "the engine stopped on the GLM usage limit"),
    "codex-usage-limit": (True, "the engine stopped on the Codex usage limit"),
    "host-auth-expired": (True, "the engine stopped on an expired host login"),
    "landing-failed": (True, "the engine ended; its landing artifact was lost"),
    "glm-key-missing": (True, "the engine never started; the tree holds no work"),
    "codex-missing": (True, "the engine never started; the tree holds no work"),
    "codex-not-logged-in": (True, "the engine never started; the tree holds no work"),
    "codex-sandbox-unavailable": (
        True,
        "the engine never started; the tree holds no work",
    ),
    "kb-absent": (True, "the engine never started; the tree holds no work"),
    "capability-unavailable": (
        True,
        "the engine never started; the tree holds no work",
    ),
    "running": (False, "the receipt is still open"),
    "code-edit-rejected": (False, "code-edit lanes keep their draft tree"),
    "code-edit-failed": (False, "code-edit lanes keep their draft tree"),
    "cloud-no-pr": (False, "cloud lane: no host run tree to retain"),
    "pr-opened": (False, "cloud lane: no host run tree to retain"),
    "no-host": (False, "never placed: no host run tree"),
    "usage": (False, "refused before placement: no host run tree"),
    "setup-failed": (False, "engine never started: retain_remote returns first"),
    "hook-parse-refused": (False, "engine never started: retain_remote returns first"),
    "claim-refused": (False, "engine never started: retain_remote returns first"),
    "claim-mirror-failed": (False, "engine never started: retain_remote returns first"),
    "ledger-refused": (False, "engine never started: retain_remote returns first"),
    "ledger-snapshot-failed": (
        False,
        "engine never started: retain_remote returns first",
    ),
    "pr-head-fetch-failed": (
        False,
        "engine never started: retain_remote returns first",
    ),
    "pr-head-stage-failed": (
        False,
        "engine never started: retain_remote returns first",
    ),
    "plugins-unresolved": (False, "engine never started: retain_remote returns first"),
    "brief-changed": (False, "refused before placement: no host run tree"),
    "ticket-text-refused": (False, "refused before placement: no host run tree"),
    "timeout-refused": (False, "refused before placement: no host run tree"),
    "prerequisite-unavailable": (False, "refused before placement: no host run tree"),
}


@pytest.mark.parametrize("status", sorted(_STATUS_TABLE))
def test_every_launcher_status_reaches_retention_or_is_kept_with_a_reason(
    symlinked, status
):
    _real, link, trees, receipt, ledger = symlinked
    reaches, reason = _STATUS_TABLE[status]
    assert reason
    assert (status in _FINAL) is reaches
    receipt = {**receipt, "status": status}
    if reaches:
        result = retain_run(link, receipt, ledger, delete=True)
        assert result["removed"] == ["rlane-ended-aaa111", "rlane-ended-aaa111.ctx"]
    else:
        with pytest.raises(ValueError, match="not final"):
            retain_run(link, receipt, ledger, delete=True)
        assert trees["rlane-ended-aaa111"][0].exists()
    assert trees["rlane-live-bbb222"][0].exists()
