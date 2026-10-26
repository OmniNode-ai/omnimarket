# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run retention preserves evidence before removing a closed lane's scratch trees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_remote_lane_effect.run_retention import retain_run


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


def test_retention_operation_resolves_from_the_packaged_contract(run):
    import importlib
    from importlib.resources import files

    import yaml

    root, tree, ctx, receipt, ledger = run
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


def test_standalone_host_payload_uses_the_tested_implementation(run):
    import subprocess

    from omnimarket.nodes.node_remote_lane_effect.run_retention import retention_script

    root, tree, ctx, receipt, ledger = run
    done = subprocess.run(
        ["/bin/sh", "-c", retention_script(str(root), receipt, ledger, delete=True)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("LANE-RETENTION ")
    assert not tree.exists()
    assert not ctx.exists()


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
