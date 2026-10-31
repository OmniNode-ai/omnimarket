# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-duplicate-check`` when every pull request of a kind regenerates the same file (OMN-21624).

On 2026-10-30 the guard refused a plan pull request because seven unrelated open plan pull requests each
rewrote the status-count rows (lines 16 and 19) of the rendered plan index, which the repository's index check
requires. The repository, its watcher state, the ledger and the three git checkouts here are real files and the
deployment overlay is a real file read by the snapshot effect. The repository is made up: the real overlay is
private data and no public test names it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.scm import scm_cli, scm_duplicate_guard

pytestmark = pytest.mark.unit

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}
REPO = "example_plans_repo"
TARGET = f"SomeOrg/{REPO}"
INDEX = "plans/INDEX.md"
EXISTING_PLAN = "plans/existing-plan.md"
OVERLAY = "node_pr_duplicate_guard_compute"
DECLARED = f"generated_paths:\n  {REPO}:\n    - {INDEX}\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(GIT_ENV),
    ).stdout.strip()


def regenerate_index(repo: Path, label: str) -> None:
    """What ``check_plan_index.py --write`` does in every plan pull request: new count rows at lines 16 and 19."""
    lines = (repo / INDEX).read_text().splitlines()
    lines[15] = f"| total | {label} |"
    lines[18] = f"| active | {label} |"
    (repo / INDEX).write_text("\n".join(lines) + "\n")


def plan_branch(
    repo: Path, branch: str, plan: str, *, edit_line: int | None = None
) -> str:
    """One commit: the plan (a new file, or a line of an existing one), the registry entry and the regenerated rows."""
    git(repo, "checkout", "-q", "-b", branch, "origin/dev")
    if edit_line is None:
        (repo / plan).write_text(f"# {plan}\n")
    else:
        lines = (repo / plan).read_text().splitlines()
        lines[edit_line - 1] = f"revised plan line {edit_line}"
        (repo / plan).write_text("\n".join(lines) + "\n")
    regenerate_index(repo, "one more plan")
    with (repo / "plans/PLAN_LIFECYCLE.yaml").open("a") as registry:
        registry.write(f"  - plan: {plan}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"plan {plan}")
    return git(repo, "rev-parse", "HEAD")


class World:
    """An origin with three open plan branches, the canonical clone, the lane's checkout and the state files."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(origin))
        git(tmp_path, "init", "-q", "-b", "dev", str(seed))
        (seed / "plans").mkdir()
        for path, count in ((INDEX, 30), (EXISTING_PLAN, 20)):
            (seed / path).write_text(
                "".join(f"{path} line {n}\n" for n in range(1, count + 1))
            )
        (seed / "plans/PLAN_LIFECYCLE.yaml").write_text("plans:\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "base")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "dev")
        git(seed, "fetch", "-q", "origin")
        self.open_prs = {
            854: ("plan/alpha-ledger", "docs: the alpha-ledger plan"),
            1538: ("plan/beta-routing", "docs: the beta-routing plan"),
            1541: ("plan/revise-existing", "docs: revise the existing plan"),
        }
        shas: dict[int, str] = {}
        shas[854] = plan_branch(seed, "plan/alpha-ledger", "plans/alpha-ledger.md")
        git(seed, "push", "-q", "origin", "plan/alpha-ledger")
        shas[1538] = plan_branch(seed, "plan/beta-routing", "plans/beta-routing.md")
        git(seed, "push", "-q", "origin", "plan/beta-routing")
        shas[1541] = plan_branch(
            seed, "plan/revise-existing", EXISTING_PLAN, edit_line=5
        )
        git(seed, "push", "-q", "origin", "plan/revise-existing")
        self.shas = shas
        self.root = tmp_path / "clones"
        self.root.mkdir()
        git(tmp_path, "clone", "-q", str(origin), str(self.root / REPO))
        self.lane = tmp_path / "lane"
        git(tmp_path, "clone", "-q", str(origin), str(self.lane))
        self.state = tmp_path / "watcher.json"
        self.ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
        self.ledger.write_text("")
        self.overlays = tmp_path / "overlays"
        self.write_state()

    def write_state(self) -> None:
        prs = {
            f"{REPO}#{number}": {
                "facts": {
                    "repo": REPO,
                    "number": number,
                    "state": "OPEN",
                    "base": "dev",
                    "head_ref": branch,
                    "head_sha": self.shas[number],
                    "title": title,
                    "body": "",
                }
            }
            for number, (branch, title) in self.open_prs.items()
        }
        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "last_tick": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "prs": prs,
                }
            )
        )

    def declare(self, text: str = DECLARED) -> None:
        (self.overlays / OVERLAY).mkdir(parents=True)
        (self.overlays / OVERLAY / "overlay.yaml").write_text(text)

    def check(self, branch: str, title: str) -> Result:
        return CliRunner().invoke(
            scm_cli.scm_group,
            [
                "pr-duplicate-check",
                TARGET,
                "--head",
                branch,
                "--base",
                "dev",
                "--title",
                title,
                "--lane",
                "plan-writer-2205",
                "--worktree",
                str(self.lane),
            ],
            catch_exceptions=False,
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(built.ledger))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(built.overlays))
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    return built


def test_a_new_plan_overlapping_only_the_regenerated_index_rows_passes(
    world: World,
) -> None:
    world.declare()
    plan_branch(world.lane, "plan/configuration-contract", "plans/config-plan.md")

    result = world.check(
        "plan/configuration-contract", "docs: the configuration contract plan"
    )

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "clear"
    assert answer["evidence"] == []


def test_without_the_declaration_the_same_plan_is_refused_on_the_index_rows(
    world: World,
) -> None:
    plan_branch(world.lane, "plan/configuration-contract", "plans/config-plan.md")

    result = world.check(
        "plan/configuration-contract", "docs: the configuration contract plan"
    )

    assert result.exit_code == scm_duplicate_guard.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "adopt"
    assert all(INDEX in e["detail"] for e in answer["evidence"])


def test_a_plan_editing_the_same_line_of_an_existing_plan_is_still_refused(
    world: World,
) -> None:
    world.declare()
    plan_branch(world.lane, "plan/second-revision", EXISTING_PLAN, edit_line=5)

    result = world.check("plan/second-revision", "docs: another revision")

    assert result.exit_code == scm_duplicate_guard.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["adopt_pr"] == f"{REPO}#1541"
    assert [e["pr"] for e in answer["evidence"]] == [f"{REPO}#1541"]
    assert f"{EXISTING_PLAN}:5" in answer["evidence"][0]["detail"]
    assert INDEX not in answer["evidence"][0]["detail"]


def test_a_plan_with_the_name_of_an_open_plan_is_still_refused(world: World) -> None:
    world.declare()
    plan_branch(world.lane, "plan/another-alpha", "plans/alpha-ledger.md")

    result = world.check("plan/another-alpha", "docs: a different title")

    assert result.exit_code == scm_duplicate_guard.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["adopt_pr"] == f"{REPO}#854"
    assert "plans/alpha-ledger.md (both add it)" in answer["evidence"][0]["detail"]


def test_an_overlay_that_cannot_be_read_checks_nothing(world: World) -> None:
    world.declare("generated_paths: [plans/INDEX.md]\n")
    plan_branch(world.lane, "plan/configuration-contract", "plans/config-plan.md")

    result = world.check(
        "plan/configuration-contract", "docs: the configuration contract plan"
    )

    assert result.exit_code == scm_duplicate_guard.EXIT_UNCHECKED, result.output
    assert json.loads(result.stdout)["state"] == "unchecked"
    assert "deployment overlay" in json.loads(result.stdout)["reason"]


def test_the_workspace_configuration_root_supplies_the_overlay_when_no_root_is_set(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS")
    config = tmp_path / "workspace-config"
    (config / "config/onex/overlays" / OVERLAY).mkdir(parents=True)
    (config / "config/onex/overlays" / OVERLAY / "overlay.yaml").write_text(DECLARED)
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config))
    plan_branch(world.lane, "plan/configuration-contract", "plans/config-plan.md")

    result = world.check(
        "plan/configuration-contract", "docs: the configuration contract plan"
    )

    assert result.exit_code == 0, result.output
