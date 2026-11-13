# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``--replaces`` against a real origin, clone and lane checkout: the measured case of 2026-11-10 (OMN-23634).

A draft install bundle (open) merged in the one commit of an earlier draft that was closed as replaced. The new
branch changes two lines that commit changed. Without ``--replaces`` the guard refuses it as a duplicate of the
bundle; with ``--replaces`` naming the closed draft it sets those overlaps aside, because every commit that
introduced the lines belongs to that draft, which the node reads as closed and not merged from the watcher state
(never from the caller). Any overlap from another commit of the bundle still refuses. Real git, real files; the
only thing absent is GitHub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.scm import scm_cli
from tests.unit.handlers.scm.test_scm_duplicate_guard import (
    commit_edit,
    git,
    invoke,
)

pytestmark = pytest.mark.unit

TARGET = "OmniNode-ai/example_repo"
SKILL = "plugins/omni/skills/lane-brief/SKILL.md"
ENGINE = "src/omni/nodes/node_lane_placement_recovery_effect/handlers/handler_lane_engine_failover.py"
OTHER = "src/omni/other.py"
NEW_BRANCH = "omn-19998-glm-lane-route"
TITLE = "feat(OMN-19998): the GLM lane route and call limits"
LANE = "dupguard-3340"
SKILL_LINE = 285
ENGINE_LINE = 163
REPLACED = f"{TARGET}#1574"
GLM_SKILL = "ROUTE glm accepted for band B2 and B3"
GLM_ENGINE = 'GLM_ENGINE = "glm"'


class World:
    """An origin with main, a closed draft (1574) and an open bundle (1643) that merged the draft's commit in."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        git(tmp_path, "init", "-q", "-b", "main", str(seed))
        for path, count in ((SKILL, 300), (ENGINE, 200), (OTHER, 20)):
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text(
                "".join(f"{path} line {n}\n" for n in range(1, count + 1))
            )
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "base")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        # The closed draft: ONE commit changing both lines.
        git(seed, "checkout", "-q", "-b", "omn-19998-glm-engine-table", "main")
        commit_edit(seed, SKILL, SKILL_LINE, GLM_SKILL, message="glm engine table")
        self.draft_sha = commit_edit(
            seed, ENGINE, ENGINE_LINE, GLM_ENGINE, message="glm engine table (code)"
        )
        git(seed, "push", "-q", "origin", "omn-19998-glm-engine-table")
        # The bundle: its own commit, the draft merged in, so the draft's commits are the bundle's too.
        git(seed, "checkout", "-q", "-b", "omn-19998-retention-stdin", "main")
        commit_edit(seed, OTHER, 5, "retention over stdin", message="retention")
        git(
            seed,
            "merge",
            "-q",
            "--no-ff",
            "omn-19998-glm-engine-table",
            "-m",
            "merge the draft",
        )
        self.bundle_sha = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", "origin", "omn-19998-retention-stdin")
        git(seed, "checkout", "-q", "main")
        self.root = tmp_path / "clones"
        self.root.mkdir()
        git(tmp_path, "clone", "-q", str(origin), str(self.root / "example_repo"))
        self.lane = tmp_path / "lane"
        git(tmp_path, "clone", "-q", str(origin), str(self.lane))
        self.state = tmp_path / "watcher.json"
        self.claims = tmp_path / "claims.md"
        # The bundle is a draft; the orchestrator's live CLAIM on the ticket is what makes its overlap count.
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.claims.write_text(
            f"{stamp} | CLAIM | lane=orchestrator-31d9 | principal=p | ticket=OMN-19998 | carrier\n"
        )
        self.write_state()
        git(self.lane, "checkout", "-q", "-b", NEW_BRANCH, "origin/main")
        commit_edit(self.lane, SKILL, SKILL_LINE, GLM_SKILL, message="lane route")
        commit_edit(self.lane, ENGINE, ENGINE_LINE, GLM_ENGINE, message="lane engine")

    def bundle_edits_again(self, path: str, line: int) -> None:
        """A later commit of the bundle that is NOT the draft's changes a line the draft changed."""
        git(self.seed, "checkout", "-q", "omn-19998-retention-stdin")
        commit_edit(self.seed, path, line, "reworded by the bundle", message="rework")
        self.bundle_sha = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "push", "-q", "origin", "omn-19998-retention-stdin")
        git(self.seed, "checkout", "-q", "main")
        git(self.root / "example_repo", "fetch", "-q", "origin")
        self.write_state()

    def write_state(
        self, *, draft_state: str = "CLOSED", draft_merged_at: str = ""
    ) -> None:
        def record(
            number: int, branch: str, sha: str, title: str, state: str, merged: str
        ) -> dict[str, Any]:
            return {
                "facts": {
                    "repo": "example_repo",
                    "number": number,
                    "state": state,
                    "merged_at": merged,
                    "base": "main",
                    "head_ref": branch,
                    "head_sha": sha,
                    "title": title,
                    "body": "",
                    "draft": True,
                }
            }

        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "last_tick": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "prs": {
                        "example_repo#1574": record(
                            1574,
                            "omn-19998-glm-engine-table",
                            self.draft_sha,
                            "feat(OMN-19998): GLM engine table",
                            draft_state,
                            draft_merged_at,
                        ),
                        "example_repo#1643": record(
                            1643,
                            "omn-19998-retention-stdin",
                            self.bundle_sha,
                            "fix(remote-lane): stream retention over stdin (OMN-19998)",
                            "OPEN",
                            "",
                        ),
                    },
                }
            )
        )

    def args(self, *replaces: str) -> list[str]:
        return [
            "pr-duplicate-check",
            TARGET,
            "--head",
            NEW_BRANCH,
            "--base",
            "main",
            "--title",
            TITLE,
            "--lane",
            LANE,
            "--worktree",
            str(self.lane),
            "--ledger",
            str(self.claims),
            *(arg for ref in replaces for arg in ("--replaces", ref)),
        ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    return built


def test_the_bundle_that_merged_in_the_draft_is_refused_without_replaces(
    world: World,
) -> None:
    """The control: with no ``--replaces`` the answer is today's (the bundle is named as the duplicate)."""
    result = invoke(world.args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "adopt"
    assert answer["adopt_pr"] == "example_repo#1643"
    assert answer["set_aside"] == []
    assert "PR-DUPLICATE-REFUSED" in result.stderr


def test_replaces_the_closed_draft_sets_its_overlaps_aside_and_allows_the_create(
    world: World,
) -> None:
    result = invoke(world.args(REPLACED))

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "clear"
    assert answer["evidence"] == []
    places = sorted(item["place"] for item in answer["set_aside"])
    assert places == sorted([f"{ENGINE}:{ENGINE_LINE}", f"{SKILL}:{SKILL_LINE}"])
    for item in answer["set_aside"]:
        assert item["pr"] == "example_repo#1643"
        assert item["replaced"] == [REPLACED]
        assert item["commits"] == [world.draft_sha[:12]] or len(item["commits"]) == 1
        assert "closed and not merged" in item["why"]
    assert "set aside" in answer["message"].casefold()
    assert "PR-DUPLICATE-GUARD clear" in result.stderr


def test_an_overlap_from_a_commit_outside_the_replaced_change_still_refuses(
    world: World,
) -> None:
    world.bundle_edits_again(ENGINE, ENGINE_LINE)

    result = invoke(world.args(REPLACED))

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "adopt"
    assert f"{ENGINE}:{ENGINE_LINE}" in answer["evidence"][0]["detail"]
    assert [item["place"] for item in answer["set_aside"]] == [f"{SKILL}:{SKILL_LINE}"]


@pytest.mark.parametrize(
    ("draft_state", "merged_at", "reason"),
    [
        ("OPEN", "", "still_open"),
        ("MERGED", "2026-11-09T10:00:00Z", "merged"),
        ("CLOSED", "2026-11-09T10:00:00Z", "merged"),
    ],
)
def test_a_replaces_entry_that_is_open_or_merged_is_refused_with_a_typed_reason(
    world: World, draft_state: str, merged_at: str, reason: str
) -> None:
    world.write_state(draft_state=draft_state, draft_merged_at=merged_at)

    result = invoke(world.args(REPLACED))

    assert result.exit_code == scm_cli.EXIT_REPLACES_REFUSED, result.output
    answer = json.loads(result.stdout)
    assert answer["state"] == "replaces_refused"
    assert [r["reason"] for r in answer["replaces_refusals"]] == [reason]
    assert answer["evidence"] == []
    assert "PR-REPLACES-REFUSED" in result.stderr


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("OmniNode-ai/omnibase_core#2159", "other_repository"),
        (f"{TARGET}#9999", "unreadable"),
    ],
)
def test_a_replaces_entry_of_another_repository_or_one_the_node_cannot_read_is_refused(
    world: World, ref: str, reason: str
) -> None:
    result = invoke(world.args(ref))

    assert result.exit_code == scm_cli.EXIT_REPLACES_REFUSED, result.output
    answer = json.loads(result.stdout)
    assert [r["reason"] for r in answer["replaces_refusals"]] == [reason]


def test_the_callers_word_is_never_the_source_of_the_state(world: World) -> None:
    """The refusal comes from the watcher's record of 1574, whatever the lane says about it."""
    world.write_state(draft_state="OPEN")

    result = invoke([*world.args(REPLACED), "--title", "replaces a CLOSED draft"])

    assert result.exit_code == scm_cli.EXIT_REPLACES_REFUSED, result.output
    assert json.loads(result.stdout)["replaces_refusals"][0]["reason"] == "still_open"


def test_a_malformed_replaces_value_is_a_usage_error(world: World) -> None:
    result = invoke(world.args("1574"))

    assert result.exit_code == 2
