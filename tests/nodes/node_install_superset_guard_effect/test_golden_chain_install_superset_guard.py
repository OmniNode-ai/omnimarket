# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Containment, intentional drops, and installer plans against scratch git repositories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_install_superset_guard_effect.handlers import (
    handler_install_superset as guard,
)
from omnimarket.nodes.node_install_superset_guard_effect.handlers import (
    handler_install_superset_cli as cli,
)
from omnimarket.nodes.node_install_superset_guard_effect.models import (
    model_install_superset as models,
)

pytestmark = pytest.mark.unit
SKILLS = "plugins/omni/skills"
CONTROLLER = f"{SKILLS}/merge-drain/scripts/landing_controller.py"


class Repo:
    """A scratch git repository whose commits each change one named file of the plugin tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.write(CONTROLLER, "# controller\n")
        self.write(f"{SKILLS}/merge-drain/SKILL.md", "# merge-drain\n")
        self.write("plugins/omni/scripts/landing_policy.py", "# policy\n")
        self.commit("main: base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args: str) -> str:
        identity = {
            "GIT_AUTHOR_NAME": "lane",
            "GIT_AUTHOR_EMAIL": "lane@example.invalid",
            "GIT_COMMITTER_NAME": "lane",
            "GIT_COMMITTER_EMAIL": "lane@example.invalid",
        }
        done = subprocess.run(
            ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            check=True,
            env={**scrub_git_location_env(os.environ), **identity},
        )
        return done.stdout.strip()

    def write(self, rel: str, body: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def change(self, branch_from: str, rel: str, message: str) -> str:
        """A commit on ``branch_from`` that adds one node file."""
        self.git("checkout", "-q", "--detach", branch_from)
        self.write(f"src/omnimarket/nodes/{rel}", f"# {message}\n")
        return self.commit(message)

    def cherry_pick(self, onto: str, *commits: str) -> str:
        self.git("checkout", "-q", "--detach", onto)
        for commit in commits:
            self.git("cherry-pick", "-x", commit)
        return self.git("rev-parse", "HEAD")


def check(
    repo: Repo,
    new: str,
    live: str = "",
    lane: str = "lane-x",
    carried: tuple[models.ModelCarriedCommit, ...] = (),
    drops: tuple[models.ModelDrop, ...] = (),
    history: str = "",
) -> models.ModelSupersetResult:
    return guard.HandlerInstallSuperset().handle(
        models.ModelSupersetRequest(
            repo=repo.root,
            new_commit=new,
            lane=lane,
            live_commit=live,
            live_carried=carried,
            drops=drops,
            history=history,
        )
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "example-repo")


def test_a_descendant_of_the_live_install_is_contained(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    b = repo.change(a, "b.py", "fix b")
    result = check(repo, b, a)
    assert result.verdict == "contained"
    assert result.held == ((a, "ancestor"),)
    assert [c.commit for c in result.carried] == [a, b]


def test_a_commit_the_new_install_lacks_is_refused_with_the_lane_that_installed_it(
    repo: Repo,
) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    other = repo.change(base, "o.py", "fix o")
    carried = (
        models.ModelCarriedCommit(
            commit=a, patch_id="", lane="lane-that-installed-a", subject="fix a"
        ),
    )
    result = check(repo, other, a, carried=carried)
    assert result.verdict == "refused"
    assert [(m.commit, m.lane, m.subject) for m in result.missing] == [
        (a, "lane-that-installed-a", "fix a")
    ]


def test_a_cherry_picked_commit_is_held_by_patch_id(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    other = repo.change(base, "o.py", "fix o")
    picked = repo.cherry_pick(other, a)
    result = check(repo, picked, a)
    assert result.verdict == "contained"
    assert result.held == ((a, "patch-id"),)


def test_a_squash_merged_commit_is_held_by_its_diff(repo: Repo) -> None:
    """Two branch commits merged to main as one squash: neither patch-id matches, both diffs are in
    the tree.
    """
    base = repo.git("rev-parse", "HEAD")
    a1 = repo.change(base, "a1.py", "fix a part 1")
    a2 = repo.change(a1, "a2.py", "fix a part 2")
    repo.git("checkout", "-q", "--detach", base)
    repo.git("merge", "-q", "--squash", a2)
    squash = repo.commit("fix a (#1)")
    later = repo.change(squash, "later.py", "later main fix")
    result = check(repo, later, a2)
    assert result.verdict == "contained"
    assert sorted(result.held) == sorted([(a1, "diff"), (a2, "diff")])


def test_a_commit_whose_change_was_since_rewritten_is_missing(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    repo.git("checkout", "-q", "--detach", base)
    repo.write("src/omnimarket/nodes/a.py", "# something else\n")
    other = repo.commit("other a")
    assert check(repo, other, a).verdict == "refused"


def test_a_drop_admits_the_install_without_that_commit(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    b = repo.change(a, "b.py", "fix b")
    other = repo.change(base, "o.py", "fix o")
    result = check(
        repo,
        other,
        b,
        drops=(models.ModelDrop(commit=a[:10], reason="superseded by o"),),
    )
    assert result.verdict == "refused"
    assert [m.commit for m in result.missing] == [b]
    result = check(
        repo,
        other,
        b,
        drops=(
            models.ModelDrop(commit=a[:10], reason="superseded"),
            models.ModelDrop(commit=b, reason="superseded"),
        ),
    )
    assert result.verdict == "dropped"
    assert [m.commit for m in result.dropped] == [a, b]


def test_a_drop_of_a_commit_that_is_not_missing_is_refused(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    b = repo.change(a, "b.py", "fix b")
    result = check(repo, b, a, drops=(models.ModelDrop(commit=a, reason="not needed"),))
    assert result.verdict == "refused"
    assert result.unused_drops == (a,)


def test_the_lane_of_an_unrecorded_commit_comes_from_the_history_text(
    repo: Repo,
) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    other = repo.change(base, "o.py", "fix o")
    history = f"OMN-0001 lane first-lane, installed ...\n\nOMN-0002 lane lane-f, installed from {a[:8]} ...\n"
    assert check(repo, other, a, history=history).missing[0].lane == "lane-f"
    assert check(repo, other, a).missing[0].lane == "unrecorded"


def test_a_live_commit_no_record_names_is_unrecorded_not_this_lanes(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    b = repo.change(a, "b.py", "fix b")
    lanes = {c.commit: c.lane for c in check(repo, b, a, lane="lane-b").carried}
    assert lanes == {a: "unrecorded", b: "lane-b"}


def test_replay_of_three_overlapping_installs(repo: Repo) -> None:
    """Two overlapping installs lose live changes; the combined install preserves them."""
    main = repo.git("rev-parse", "HEAD")
    pool_read = repo.change(main, "pool_read.py", "placement lock pool read")
    engine = repo.change(
        pool_read, "codex_engine.py", "Codex where no Claude host has headroom"
    )
    restore = repo.change(engine, "restore_src.py", "restore source")
    initial = repo.change(restore, "not_started.py", "not-started leases")
    hold = repo.change(initial, "external_hold.py", "external-blocker hold")
    hold_read = repo.change(hold, "cause_result_read.py", "validator-tests fix")
    claims_close = repo.change(initial, "lease_claim_close.py", "lease claim close")
    wait_reasons = repo.change(
        hold, "wait_reasons.py", "node_landing_wait_reasons_compute"
    )
    picked = repo.cherry_pick(claims_close, hold, hold_read)
    repo.git("merge", "-q", "--no-edit", "--no-ff", wait_reasons)
    combined = repo.git("rev-parse", "HEAD")
    live, carried = ("", ())

    def install(
        new: str, lane: str, drops: tuple[models.ModelDrop, ...] = ()
    ) -> models.ModelSupersetResult:
        nonlocal live, carried
        result = check(repo, new, live, lane=lane, carried=carried, drops=drops)
        if result.verdict != "refused":
            live, carried = (result.new_commit, result.carried)
        return result

    assert install(initial, "lane-a").verdict == "contained"
    assert install(hold, "lane-b").verdict == "contained"
    assert install(hold_read, "lane-b").verdict == "contained"
    assert {c.subject: c.lane for c in carried}["placement lock pool read"] == "lane-a"
    first = install(claims_close, "lane-c")
    assert first.verdict == "refused"
    assert [(m.commit, m.lane) for m in first.missing] == [
        (hold, "lane-b"),
        (hold_read, "lane-b"),
    ]
    assert live == hold_read
    went_in = install(
        claims_close,
        "lane-c",
        drops=(
            models.ModelDrop(commit=hold, reason="replay"),
            models.ModelDrop(commit=hold_read, reason="replay"),
        ),
    )
    assert went_in.verdict == "dropped"
    assert live == claims_close
    second = install(wait_reasons, "lane-d")
    assert second.verdict == "refused"
    assert [(m.commit, m.lane) for m in second.missing] == [(claims_close, "lane-c")]
    third = install(combined, "lane-e")
    assert third.verdict == "contained"
    assert live == combined
    assert picked != combined
    lanes = {c.subject: c.lane for c in carried}
    assert lanes["lease claim close"] == "lane-c"
    assert lanes["node_landing_wait_reasons_compute"] == "lane-e"
    over_hold_read = check(repo, combined, hold_read)
    assert over_hold_read.verdict == "contained"
    assert dict(over_hold_read.held) == {hold_read: "patch-id"}


def installed_tree(repo: Repo, commit: str, install_dir: Path) -> None:
    """The live tree install.sh leaves for ``commit``: the plugin's skills/ and scripts/."""
    for rel in repo.git(
        "ls-tree", "-r", "--name-only", commit, "plugins/omni/"
    ).splitlines():
        part = "skills" if rel.startswith(SKILLS + "/") else "scripts"
        base = SKILLS if part == "skills" else "plugins/omni/scripts"
        dest = install_dir / part / rel[len(base) + 1 :]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(
            subprocess.run(
                ["git", "-C", str(repo.root), "show", f"{commit}:{rel}"],
                capture_output=True,
                check=True,
                env=scrub_git_location_env(os.environ),
            ).stdout
        )


def cli_check(repo: Repo, tmp_path: Path, *extra: str) -> tuple[int, Path]:
    plan = tmp_path / "plan.json"
    code = cli.main(
        [
            "check",
            "--install-dir",
            str(tmp_path / "install"),
            "--state-dir",
            str(tmp_path / "state"),
            "--source",
            str(repo.root / SKILLS),
            "--lane",
            "lane-new",
            "--history",
            str(tmp_path / "install/INSTALLED-FROM-BRANCHES.txt"),
            "--plan",
            str(plan),
            "--drop-rows",
            str(tmp_path / "drop-rows.txt"),
            *extra,
        ]
    )
    return (code, plan)


def test_cli_refuses_an_install_that_lacks_the_live_commit_and_writes_a_drop_row(
    repo: Repo,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = repo.git("rev-parse", "HEAD")
    a = repo.change(base, "a.py", "fix a")
    repo.git("checkout", "-q", "--detach", base)
    repo.write(CONTROLLER, "# controller with o\n")
    repo.commit("fix o")
    install_dir = tmp_path / "install"
    installed_tree(repo, a, install_dir)
    (install_dir / "landing_install.json").write_text(
        json.dumps({"branch_until": "2999-01-01T00:00:00Z"}), "utf-8"
    )
    code, _ = cli_check(repo, tmp_path)
    assert code == 3
    assert "LIVE-UNRECORDED" in capsys.readouterr().err
    code, plan = cli_check(repo, tmp_path, "--live-commit", a)
    err = capsys.readouterr().err
    assert code == 3
    assert f"INSTALL-SUPERSET-MISSING {a[:12]} lane=unrecorded fix a" in err
    assert not plan.exists()
    overlay = tmp_path / "overlays" / "plugin_install"
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text("ledger_ticket: OMN-0000\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent))
    ledger = tmp_path / "drop-rows.txt"
    code, plan = cli_check(
        repo,
        tmp_path,
        "--live-commit",
        a,
        "--drop",
        a[:9],
        "--reason",
        "superseded by o",
    )
    assert code == 0, capsys.readouterr().err
    row = ledger.read_text("utf-8").splitlines()[-1]
    assert "INSTALL-SUPERSET-DROP target=landing-controller" in row
    assert f"commit={a}" in row
    assert "lane=lane-new" in row
    assert "installed_by=unrecorded" in row
    assert "superseded by o" in row
    assert "ticket=OMN-0000" in row
    recorded = json.loads(plan.read_text("utf-8"))
    assert recorded["over"] == a
    assert recorded["dropped"][0]["commit"] == a
    record = install_dir / "landing_install.json"
    history = install_dir / "INSTALLED-FROM-BRANCHES.txt"
    assert (
        cli.main(
            [
                "record",
                "--plan",
                str(plan),
                "--install-record",
                str(record),
                "--history",
                str(history),
            ]
        )
        == 0
    )
    assert (
        json.loads(record.read_text("utf-8"))["superset"]["commit"]
        == recorded["commit"]
    )
    assert f"commit={recorded['commit']} over={a}" in history.read_text("utf-8")


def test_cli_refuses_a_live_tree_that_is_not_its_recorded_commit(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "--detach", base)
    repo.write(CONTROLLER, "# controller b\n")
    b = repo.commit("controller b")
    install_dir = tmp_path / "install"
    installed_tree(repo, base, install_dir)
    (install_dir / "landing_install.json").write_text(
        json.dumps({"superset": {"commit": b}}), "utf-8"
    )
    code, _ = cli_check(repo, tmp_path)
    err = capsys.readouterr().err
    assert code == 3
    assert "LIVE-TREE-MISMATCH" in err
    assert "landing_controller.py" in err


def test_cli_refuses_a_dirty_source(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("checkout", "-q", "main")
    repo.write(f"{SKILLS}/merge-drain/scripts/extra.py", "# not committed\n")
    code, _ = cli_check(repo, tmp_path)
    assert code == 3
    assert "BRANCH-SOURCE-DIRTY" in capsys.readouterr().err


def test_the_tree_comparison_reads_only_what_the_installer_copies(
    repo: Repo, tmp_path: Path
) -> None:
    base = repo.git("rev-parse", "HEAD")
    install_dir = tmp_path / "install"
    installed_tree(repo, base, install_dir)
    stale = install_dir / "skills/merge-drain/landing_floors.json"
    stale.write_text("{}\n", encoding="utf-8")
    compared, differ = guard.tree_differences(repo.root, base, SKILLS, install_dir)
    assert compared >= 2
    assert differ == []
    (install_dir / "skills/merge-drain/scripts/landing_controller.py").write_text(
        "# hot-patched\n", "utf-8"
    )
    assert guard.tree_differences(repo.root, base, SKILLS, install_dir)[1] == [
        "skills/merge-drain/scripts/landing_controller.py"
    ]


def test_cli_drop_requires_ledger_ticket_overlay(
    repo: Repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    base = repo.git("rev-parse", "HEAD")
    live = repo.change(base, "a.py", "fix a")
    repo.change(base, "b.py", "fix b")
    install_dir = tmp_path / "install"
    installed_tree(repo, live, install_dir)
    code, plan = cli_check(
        repo, tmp_path, "--live-commit", live, "--drop", live, "--reason", "superseded"
    )
    assert code == 3
    assert "ledger_ticket" in capsys.readouterr().err
    assert not plan.exists()
    assert not (tmp_path / "drop-rows.txt").exists()


def test_cli_no_drops_needs_no_overlay(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    code, plan = cli_check(repo, tmp_path)
    assert code == 0
    assert plan.is_file()
    assert (tmp_path / "drop-rows.txt").read_text() == ""


def test_branch_without_guard_preserves_it_before_src_swap_and_records(
    repo: Repo, tmp_path: Path
) -> None:
    """The second source lacks the guard; record still imports from the new live src."""
    from omnimarket.nodes.node_install_only_preserve_effect import (
        NodeInstallOnlyPreserveEffect,
    )
    from omnimarket.nodes.node_install_only_preserve_effect.models import (
        ModelInstallOnlyOverlay,
        ModelInstallOnlyRequest,
    )

    # The first source contains the real guard under the installer's package name.
    relative = "src/omni/nodes/node_install_superset_guard_effect"
    source = tmp_path / "source"
    guard_source = Path(guard.__file__).parents[1]
    shutil.copytree(
        guard_source, source / relative, ignore=shutil.ignore_patterns("__pycache__")
    )
    live = tmp_path / "live"
    shutil.copytree(source / "src", live / "src")
    shutil.rmtree(source / relative)
    stage = live / ".install-stage.test"
    shutil.copytree(source / "src", stage / "src")
    assert not (stage / relative).exists()

    node = NodeInstallOnlyPreserveEffect(
        overlay=ModelInstallOnlyOverlay(nodes=(relative,))
    )
    result = node.handle(ModelInstallOnlyRequest(live_root=live, stage_root=stage))
    assert result.preserved == (relative,)
    (live / "src").rename(stage / "src.replaced")
    (stage / "src").rename(live / "src")
    assert (live / relative / "handlers/handler_install_superset_cli.py").is_file()

    # A positive control uses the real guard's check and record CLI after the swap.
    # The commit subject is the recorded incident's marker, not preservation output.
    base = repo.git("rev-parse", "HEAD")
    first = repo.change(base, "guard.py", "guard=first")
    env = {
        **scrub_git_location_env(os.environ),
        "PYTHONPATH": os.pathsep.join(
            (str(live / "src"), str(guard_source.parents[2]))
        ),
    }
    module = "omni.nodes.node_install_superset_guard_effect.handlers.handler_install_superset_cli"
    plan, history = tmp_path / "plan.json", live / "INSTALLED-FROM-BRANCHES.txt"
    command = [sys.executable, "-m", module]
    checked = subprocess.run(
        [
            *command,
            "check",
            "--install-dir",
            str(live),
            "--state-dir",
            str(tmp_path / "state"),
            "--source",
            str(repo.root / SKILLS),
            "--lane",
            "second",
            "--history",
            str(history),
            "--plan",
            str(plan),
            "--drop-rows",
            str(tmp_path / "drops.txt"),
            "--main-ref",
            base,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "INSTALL-SUPERSET-CONTAINED" in checked.stdout
    recorded = subprocess.run(
        [
            *command,
            "record",
            "--plan",
            str(plan),
            "--install-record",
            str(live / "landing_install.json"),
            "--history",
            str(history),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert recorded.returncode == 0, recorded.stderr
    entry = json.loads((live / "landing_install.json").read_text())["superset"]
    assert entry["commit"] == first
    assert entry["carried"][0]["subject"] == "guard=first"
    assert "SUPERSET" in history.read_text()


def test_preservation_cli_copies_all_declared_nodes_and_keeps_stage_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_install_only_preserve_effect.__main__ import main

    live, stage = tmp_path / "live", tmp_path / "stage"
    for root, node, body in [
        (live, "first", "live first"),
        (live, "second", "live second"),
        (stage, "second", "stage second"),
        (live, "undeclared", "private"),
    ]:
        target = root / "src/example/nodes" / node / "handler.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    overlay = tmp_path / "overlays/node_install_only_preserve_effect"
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text(
        "nodes: [src/example/nodes/first, src/example/nodes/second, src/example/nodes/absent]\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent))
    assert main(["--install-root", str(live), "--stage-root", str(stage)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preserved"] == ["src/example/nodes/first"]
    assert result["stage_present"] == ["src/example/nodes/second"]
    assert result["unavailable"] == ["src/example/nodes/absent"]
    assert (stage / "src/example/nodes/first/handler.py").read_text() == "live first"
    assert (stage / "src/example/nodes/second/handler.py").read_text() == "stage second"
    assert not (stage / "src/example/nodes/undeclared").exists()
    assert main(["--install-root", str(live), "--stage-root", str(stage)]) == 0
    assert json.loads(capsys.readouterr().out)["preserved"] == []


@pytest.mark.parametrize(
    "declarations",
    [
        [],
        ["/absolute/node"],
        ["../outside/node"],
        ["src/../node"],
        ["src/./node"],
        ["src//node"],
        ["src/node/"],
        ["src/node", "src/node"],
        ["src/node", "src/node/nested"],
    ],
)
def test_preservation_rejects_invalid_declarations(declarations: list[str]) -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_install_only_preserve_effect.models import (
        ModelInstallOnlyOverlay,
    )

    with pytest.raises(ValidationError):
        ModelInstallOnlyOverlay(nodes=declarations)


def test_preservation_requires_overlay_and_refuses_unsafe_stage_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_install_only_preserve_effect import (
        NodeInstallOnlyPreserveEffect,
    )
    from omnimarket.nodes.node_install_only_preserve_effect.__main__ import main
    from omnimarket.nodes.node_install_only_preserve_effect.models import (
        ModelInstallOnlyOverlay,
        ModelInstallOnlyRequest,
    )

    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.delenv("INSTALL_ONLY_NODES_OVERLAY", raising=False)
    live, stage, outside = (tmp_path / name for name in ("live", "stage", "outside"))
    for root in (live, stage, outside):
        root.mkdir()
    assert main(["--install-root", str(live), "--stage-root", str(stage)]) == 3
    assert "nodes" in capsys.readouterr().err
    for relative in ("src/nodes/first", "src/nodes/second"):
        target = live / relative / "handler.py"
        target.parent.mkdir(parents=True)
        target.write_text("first")
    (stage / "src").symlink_to(outside, target_is_directory=True)
    node = NodeInstallOnlyPreserveEffect(
        overlay=ModelInstallOnlyOverlay(nodes=("src/nodes/first", "src/nodes/second"))
    )
    with pytest.raises(ValueError, match=r"escapes|symlink"):
        node.handle(ModelInstallOnlyRequest(live_root=live, stage_root=stage))
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError, match="equal or contain"):
        node.handle(ModelInstallOnlyRequest(live_root=live, stage_root=live))
    with pytest.raises(ValueError, match="equal or contain"):
        node.handle(ModelInstallOnlyRequest(live_root=live, stage_root=tmp_path))


def test_preservation_uses_injected_copy_and_failed_copy_leaves_no_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_install_only_preserve_effect import (
        NodeInstallOnlyPreserveEffect,
    )
    from omnimarket.nodes.node_install_only_preserve_effect import (
        handler as preservation,
    )
    from omnimarket.nodes.node_install_only_preserve_effect.models import (
        ModelInstallOnlyOverlay,
        ModelInstallOnlyRequest,
    )

    live, stage = tmp_path / "live", tmp_path / "stage"
    (live / "src/nodes/guard").mkdir(parents=True)
    stage.mkdir()
    calls: list[tuple[Path, Path]] = []
    handler = preservation.HandlerInstallOnlyPreserve(
        overlay=ModelInstallOnlyOverlay(nodes=("src/nodes/guard",)),
        copy_tree=lambda source, dest: calls.append((source, dest)),
    )
    node = NodeInstallOnlyPreserveEffect(handler=handler)
    request = ModelInstallOnlyRequest(live_root=live, stage_root=stage)
    assert node.handle(request).preserved == ("src/nodes/guard",)
    assert calls == [(live / "src/nodes/guard", stage / "src/nodes/guard")]

    def failed_copy(source: Path, target: Path) -> None:
        target.mkdir()
        (target / "partial.py").write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr(preservation.shutil, "copytree", failed_copy)
    with pytest.raises(OSError, match="copy failed"):
        preservation.copy_node(*calls[0])
    assert not (stage / "src/nodes/guard").exists()
    assert list((stage / "src/nodes").iterdir()) == []


def test_recorded_guard_first_case_from_the_second_install(tmp_path: Path) -> None:
    """Minimal guard sentinel reproduces the recorded check/record import failure."""
    relative = "src/omni/nodes/node_install_superset_guard_effect"
    source, live = tmp_path / "source", tmp_path / "live"
    package = source / relative
    package.mkdir(parents=True)
    (package / "__main__.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[1] == 'check':\n print('INSTALL-SUPERSET-CONTAINED guard=first')\n"
        "else:\n Path(sys.argv[2]).write_text('SUPERSET guard=first\\n')\n"
    )
    shutil.copytree(source / "src", live / "src")
    shutil.rmtree(package)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(f"nodes: [{relative}]\n")
    stage = live / ".install-stage.second"
    shutil.copytree(source / "src", stage / "src")
    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "omni.nodes.node_install_superset_guard_effect",
            "record",
        ],
        env={**os.environ, "PYTHONPATH": str(stage / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert missing.returncode != 0
    assert "No module named" in missing.stderr
    # Invoke the same single module call the shell installer will use.
    node_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_install_only_preserve_effect",
            "--install-root",
            str(live),
            "--stage-root",
            str(stage),
        ],
        env={
            **os.environ,
            "INSTALL_ONLY_NODES_OVERLAY": str(overlay),
            "PYTHONPATH": str(Path(guard.__file__).parents[4]),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert node_cli.returncode == 0, node_cli.stderr
    assert json.loads(node_cli.stdout)["preserved"] == [relative]
    (live / "src").rename(stage / "src.replaced")
    (stage / "src").rename(live / "src")
    assert (live / relative / "__main__.py").is_file()
    env = {**os.environ, "PYTHONPATH": str(live / "src")}
    command = [sys.executable, "-m", "omni.nodes.node_install_superset_guard_effect"]
    checked = subprocess.run(
        [*command, "check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert "INSTALL-SUPERSET-CONTAINED guard=first" in checked.stdout
    history = live / "INSTALLED-FROM-BRANCHES.txt"
    recorded = subprocess.run(
        [*command, "record", str(history)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert recorded.returncode == 0, recorded.stderr
    assert "SUPERSET guard=first" in history.read_text()


async def test_preservation_runtime_command_and_completed_event(tmp_path: Path) -> None:
    import asyncio
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_install_only_preserve_effect import (
        NodeInstallOnlyPreserveEffect,
    )

    contract = NodeInstallOnlyPreserveEffect.contract_path
    manifest = discover_contracts_from_paths([contract])
    assert not manifest.errors
    assert len(manifest.contracts) == 1
    raw = yaml.safe_load(contract.read_text())
    command, completed = raw["runtime_dispatch"]["command_topic"], raw["terminal_event"]
    assert completed == "onex.evt.omnimarket.install-only-preserve-completed.v1"
    live, stage = tmp_path / "live", tmp_path / "stage"
    (live / "src/example/nodes/guard").mkdir(parents=True)
    (live / "src/example/nodes/guard/handler.py").write_text("first")
    stage.mkdir()
    results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="install-only-preserve")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await results.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed, group_id="install-only-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="local"
        )
        assert [result.outcome.value for result in report.results] == ["wired"]
        engine.freeze()
        correlation_id = uuid4()
        envelope = ModelEventEnvelope[object](
            payload={
                "live_root": str(live),
                "stage_root": str(stage),
                "overlay": {"nodes": ["src/example/nodes/guard"]},
            },
            correlation_id=correlation_id,
            event_type="omnimarket.install-only-preserve-requested",
        )
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        event = await asyncio.wait_for(results.get(), timeout=3)
        assert event["correlation_id"] == str(correlation_id)
        assert event["payload"]["preserved"] == ["src/example/nodes/guard"]
        assert (stage / "src/example/nodes/guard/handler.py").read_text() == "first"
    finally:
        await bus.close()
