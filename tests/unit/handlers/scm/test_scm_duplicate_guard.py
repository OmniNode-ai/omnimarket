# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-duplicate-check`` and ``pr-create`` against a real origin, clone and lane checkout (OMN-17427).

The fixture is the shape of 2026-10-30: a pull request is open on a protocol-binding baseline rename, and a
second lane has a branch renaming the same line. The PR watcher state, the ledger and the three git
checkouts are real files; nothing is mocked but the bus call ``pr-create`` makes after the guard passes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.scm import scm_cli
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    parse_diff,
)

pytestmark = pytest.mark.unit

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}
BASELINE = ".onex_ratchets/protocol_binding_baseline.txt"
TARGET = "OmniNode-ai/omnibase_core"
NEW_BRANCH = "OMN-21518-protocol-binding-baseline-rename"
TITLE = "fix(OMN-21518): protocol_binding baseline names the file that now holds the construction"
LANE = "core-dev-protocol-baseline-0030"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(GIT_ENV),
    ).stdout.strip()


def commit_edit(repo: Path, path: str, line: int, text: str) -> str:
    target = repo / path
    lines = target.read_text().splitlines()
    lines[line - 1] = text
    target.write_text("\n".join(lines) + "\n")
    git(repo, "commit", "-qam", f"edit {path}:{line}")
    return git(repo, "rev-parse", "HEAD")


class World:
    """An origin with dev and two open PR branches, the canonical clone, the lane's checkout, state files."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(origin))
        git(tmp_path, "init", "-q", "-b", "dev", str(seed))
        for path, count in (
            (BASELINE, 12),
            ("tests/test_binding.py", 80),
            ("src/metrics.py", 20),
        ):
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text(
                "".join(f"{path} line {n}\n" for n in range(1, count + 1))
            )
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "base")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "dev")
        self.shas: dict[int, str] = {}
        for number, branch, edits in (
            (
                2159,
                "fix/omn-21533-protocol-binding-race",
                ((BASELINE, 9), ("tests/test_binding.py", 70)),
            ),
            (2170, "feat/omn-21600-metrics", (("src/metrics.py", 3),)),
        ):
            git(seed, "checkout", "-q", "-b", branch, "dev")
            for path, line in edits:
                self.shas[number] = commit_edit(
                    seed, path, line, f"{branch} rewrote {line}"
                )
            git(seed, "push", "-q", "origin", branch)
            git(seed, "checkout", "-q", "dev")
        self.root = tmp_path / "clones"
        self.root.mkdir()
        git(tmp_path, "clone", "-q", str(origin), str(self.root / "omnibase_core"))
        self.lane = tmp_path / "lane"
        git(tmp_path, "clone", "-q", str(origin), str(self.lane))
        self.state = tmp_path / "watcher.json"
        self.ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
        self.ledger.write_text("")
        self.write_state(datetime.now(UTC))

    def write_state(self, tick: datetime) -> None:
        def record(number: int, branch: str, title: str) -> dict[str, Any]:
            return {
                "facts": {
                    "repo": "omnibase_core",
                    "number": number,
                    "state": "OPEN",
                    "base": "dev",
                    "head_ref": branch,
                    "head_sha": self.shas[number],
                    "title": title,
                    "body": "",
                }
            }

        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "last_tick": tick.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "prs": {
                        "omnibase_core#2159": record(
                            2159,
                            "fix/omn-21533-protocol-binding-race",
                            "OMN-21533: follow the ci_inheritance runtime move in the protocol_binding baseline",
                        ),
                        "omnibase_core#2170": record(
                            2170,
                            "feat/omn-21600-metrics",
                            "feat(OMN-21600): depth metrics",
                        ),
                    },
                }
            )
        )

    def lane_branch(self, path: str, line: int) -> None:
        git(self.lane, "checkout", "-q", "-b", NEW_BRANCH, "origin/dev")
        commit_edit(self.lane, path, line, "renamed to the node path")

    def check_args(self) -> list[str]:
        return [
            "pr-duplicate-check",
            TARGET,
            "--head",
            NEW_BRANCH,
            "--base",
            "dev",
            "--title",
            TITLE,
            "--lane",
            LANE,
            "--worktree",
            str(self.lane),
        ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(built.ledger))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    return built


def invoke(args: list[str]) -> Result:
    return CliRunner().invoke(scm_cli.scm_group, args, catch_exceptions=False)


def test_a_second_rename_of_the_same_baseline_line_is_refused_with_the_first_named(
    world: World,
) -> None:
    world.lane_branch(BASELINE, 9)

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "adopt"
    assert answer["adopt_pr"] == "omnibase_core#2159"
    assert f"{BASELINE}:9" in answer["evidence"][0]["detail"]
    assert "PR-DUPLICATE-REFUSED" in result.stderr
    assert "adopted=omnibase_core#2159" in result.stderr


def test_an_unrelated_pull_request_in_the_same_repository_passes(
    world: World,
) -> None:
    world.lane_branch("src/metrics.py", 15)  # #2170 changed line 3 of the same file

    result = invoke(world.check_args())

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "clear"
    assert any("omnibase_core#2170" in note for note in answer["notes"])


def test_a_branch_the_lane_never_made_cannot_be_checked(world: World) -> None:
    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert json.loads(result.stdout)["state"] == "unchecked"


def test_a_watcher_that_stopped_ticking_checks_nothing(world: World) -> None:
    world.lane_branch("src/metrics.py", 15)
    world.write_state(datetime.now(UTC) - timedelta(hours=2))

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "last_tick" in json.loads(result.stdout)["reason"]


def test_an_unset_ledger_path_checks_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.lane_branch("src/metrics.py", 15)
    monkeypatch.delenv("ONEX_LEDGER_PATH")

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "ONEX_LEDGER_PATH" in json.loads(result.stdout)["reason"]


def test_a_live_claim_with_the_same_cause_cell_is_refused_with_its_lane_named(
    world: World,
) -> None:
    world.lane_branch("src/metrics.py", 15)
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cause = "cause=omnibase_core:Pre-commit Suite"
    world.ledger.write_text(
        f"{stamp} | CLAIM | lane=cause-core-0010 | principal=p | {cause} | pr=omnibase_core#2159 | fix\n"
        f"{stamp} | CLAIM | lane={LANE} | principal=p | {cause} | mine\n"
    )

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["adopt_lane"] == "cause-core-0010"
    assert answer["adopt_pr"] == "omnibase_core#2159"


# ---- onex scm pr-create


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def fake(*args: object, **kwargs: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(scm_cli, "_call_bus", fake)
    return calls


CREATE_ARGS = [
    "pr-create",
    TARGET,
    "--head",
    NEW_BRANCH,
    "--base",
    "dev",
    "--title",
    TITLE,
    "--body",
    "body",
    "--lane",
    LANE,
]


def test_pr_create_opens_nothing_when_the_guard_refuses(
    world: World, sent: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    world.lane_branch(BASELINE, 9)
    monkeypatch.chdir(world.lane)

    result = invoke(CREATE_ARGS)

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    assert sent == []
    assert "omnibase_core#2159" in result.stderr


def test_pr_create_dry_run_is_guarded_too(
    world: World, sent: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    world.lane_branch(BASELINE, 9)
    monkeypatch.chdir(world.lane)

    result = invoke([*CREATE_ARGS, "--dry-run"])

    assert result.exit_code == scm_cli.EXIT_DUPLICATE
    assert sent == []


def test_pr_create_goes_on_to_the_bus_when_the_guard_reads_clear(
    world: World, sent: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    world.lane_branch("src/metrics.py", 15)
    monkeypatch.chdir(world.lane)

    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main(CREATE_ARGS, standalone_mode=False)

    assert stopped.value.code == 0
    assert len(sent) == 1


def test_pr_create_opens_nothing_when_the_guard_cannot_read(
    world: World, sent: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    world.lane_branch("src/metrics.py", 15)
    monkeypatch.chdir(world.lane)
    world.write_state(datetime.now(UTC) - timedelta(hours=2))

    result = invoke(CREATE_ARGS)

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert sent == []
    assert "PR-DUPLICATE-GUARD unchecked" in result.stderr


# ---- the diff parser


def test_parse_diff_keeps_replaced_lines_and_ignores_pure_insertions() -> None:
    diff = "\n".join(
        [
            "diff --git a/a.txt b/a.txt",
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -9 +9 @@ ctx",
            "@@ -20,3 +20,4 @@",
            "@@ -30,0 +31,2 @@",
            "diff --git a/new file.txt b/new file.txt",
            "new file mode 100644",
            "@@ -0,0 +1,3 @@",
            "diff --git a/b.bin b/b.bin",
            "Binary files a/b.bin and b/b.bin differ",
        ]
    )

    touch = {f.path: f for f in parse_diff(diff).files}

    assert touch["a.txt"].spans == ((9, 9), (20, 22))
    assert touch["new file.txt"].added
    assert touch["new file.txt"].spans == ()
    assert touch["b.bin"].spans == ()
