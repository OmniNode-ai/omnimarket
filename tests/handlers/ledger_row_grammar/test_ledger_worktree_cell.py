# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Dropped test_the_lane_rules_block_carries_the_cell_rule: requires an external lane-rules document.
# Rewrote test_e2e_the_append_path_enforces_the_cells; dropped retired append command I/O.
"""CLAIM worktrees and truthful TERMINAL dispositions in temporary registries."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import cast

import pytest

from omnimarket.handlers import ledger_row_grammar as g
from omnimarket.handlers import ledger_worktree_cell as worktree

pytestmark = pytest.mark.unit
REGISTRY_ENV = "OMNI_HOME"
LEDGER_NAME = "ROLLING_WORK_LEDGER.md"
CLAIM_AT = "2026-09-30T10:00:00Z"
TERMINAL_AT = "2026-09-30T11:00:00Z"
COST = "est ~1 lane-hours; displaces nothing; (OMN-20148)"
WT = f"${REGISTRY_ENV}/omni_worktrees/OMN-20148/repo"


def claim(
    *cells: str, lane: str = "lane-a", at: str = CLAIM_AT, extra: str = ""
) -> str:
    joined = " | ".join(cells)
    head = f"{at} | CLAIM | lane={lane} | ticket=OMN-20148 | actor=claude:test"
    return f"{head} | {joined} | {COST}{extra}" if joined else f"{head} | {COST}{extra}"


def terminal(*cells: str, lane: str = "lane-a", at: str = TERMINAL_AT) -> str:
    joined = " | ".join(cells)
    head = f"{at} | TERMINAL | lane={lane} | ticket=OMN-20148 | friction=none | delegation=na:test"
    return f"{head} | {joined} | done" if joined else f"{head} | done"


def append_refusal(existing: str, payload: str) -> str | None:
    """What onex-ledger decides: the pure half first, then the state half."""
    return g.refusal_for_payload(payload, LEDGER_NAME) or g.refusal_for_state(
        payload, existing, LEDGER_NAME
    )


def test_claim_without_a_worktree_cell_is_refused() -> None:
    reasons = g.row_refusals(claim())
    assert any("no worktree cell" in r and "OMN-20148" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "cell", ["worktree=none", f"worktree={WT}", "worktree=/srv/wt/a/b"]
)
def test_claim_with_a_worktree_path_or_none_is_accepted(cell: str) -> None:
    assert g.row_refusals(claim(cell)) == []


@pytest.mark.parametrize(
    ("cell", "fragment"),
    [
        ("worktree=", "no worktree cell"),
        ("worktree=n/a", "placeholder"),
        ("worktree=-", "placeholder"),
        ("worktree=repo", "not a path"),
        ("worktree=omni_worktrees/OMN-1/<repo>", "placeholder"),
        ("worktree=omni_worktrees/OMN-1/a b", "whitespace"),
        ("worktree=omni_worktrees/OMN-1/a,omni_worktrees/OMN-1/b", "comma"),
    ],
)
def test_claim_with_a_malformed_worktree_cell_is_refused(
    cell: str, fragment: str
) -> None:
    reasons = " ".join(g.row_refusals(claim(cell)))
    assert fragment in reasons, reasons


def test_claim_with_two_worktree_cells_is_refused() -> None:
    reasons = " ".join(g.row_refusals(claim("worktree=none", "worktree=none")))
    assert "more than one worktree" in reasons


@pytest.mark.parametrize("identity", ["actor=script:landing-controller", "model=none"])
def test_a_script_claim_is_exempt_from_the_required_cell(identity: str) -> None:
    line = f"{CLAIM_AT} | CLAIM | lane=lane-b | ticket=OMN-20148 | {identity} | {COST}"
    assert g.row_refusals(line) == []
    bad = f"{line} | worktree=repo"
    assert any("not a path" in r for r in g.row_refusals(bad))


def test_the_grammar_lists_worktree_as_required_on_a_claim() -> None:
    assert "worktree" in cast("Collection[str]", g.GRAMMAR["CLAIM"]["required"])
    assert "worktree" in cast("Collection[str]", g.GRAMMAR["TERMINAL"]["optional"])


def test_terminal_cell_is_refused_when_the_closed_claim_named_a_worktree(
    registry: Path,
) -> None:
    existing = claim(f"worktree={WT}") + "\n"
    reason = append_refusal(existing, terminal())
    assert reason is not None
    assert "OMN-20148" in reason
    assert "no worktree cell" in reason
    assert WT in reason


def test_terminal_without_a_cell_is_accepted_when_the_claim_said_none(
    registry: Path,
) -> None:
    existing = claim("worktree=none") + "\n"
    assert append_refusal(existing, terminal()) is None


def test_terminal_without_a_cell_is_accepted_when_no_claim_can_be_found(
    registry: Path,
) -> None:
    assert append_refusal("", terminal()) is None
    assert (
        append_refusal(claim(f"worktree={WT}") + "\n", terminal(lane="lane-b")) is None
    )


def test_terminal_cell_is_found_through_closes_claim_and_not_the_latest_claim(
    registry: Path,
) -> None:
    first = claim(f"worktree={WT}", at="2026-09-30T09:00:00Z")
    second = claim("worktree=none", at="2026-09-30T09:30:00Z")
    existing = f"{first}\n{second}\n"
    plain = append_refusal(existing, terminal())
    assert plain is None, "the newest claim of the lane said none"
    naming = append_refusal(existing, terminal("closes-CLAIM=2026-09-30T09:00:00Z"))
    assert naming is not None
    assert "no worktree cell" in naming


def test_a_script_terminal_is_exempt_from_the_required_cell(registry: Path) -> None:
    existing = claim(f"worktree={WT}") + "\n"
    line = (
        f"{TERMINAL_AT} | TERMINAL | lane=lane-a | ticket=OMN-20148 | actor=script:landing "
        "| friction=none | done"
    )
    assert g.refusal_for_state(line, existing, LEDGER_NAME) is None


@pytest.mark.parametrize("field", ["closes-CLAIM", "closes"])
@pytest.mark.parametrize("ticket", ["OMN-20148", "OMN-1"])
def test_terminal_naming_a_rolled_claim_does_not_close_another_live_claim(
    registry: Path, field: str, ticket: str
) -> None:
    existing = (
        claim(f"worktree={WT}").replace("ticket=OMN-20148", f"ticket={ticket}") + "\n"
    )
    row = terminal(f"{field}=2026-09-29T09:00:00Z", "worktree=none")
    assert append_refusal(existing, row) is None


def test_a_tickets_claim_of_the_same_lane_is_preferred_to_a_newer_other_ticket(
    registry: Path,
) -> None:
    ours = claim(f"worktree={WT}", at="2026-09-30T09:00:00Z")
    other = (
        "2026-09-30T09:30:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | actor=x | "
        f"worktree=none | {COST}"
    )
    reason = append_refusal(f"{ours}\n{other}\n", terminal())
    assert reason is not None
    assert "no worktree cell" in reason


def test_disposition_removed_is_refused_while_the_path_exists_and_accepted_after_removal(
    registry: Path,
) -> None:
    tree = registry / "omni_worktrees" / "OMN-20148" / "repo"
    tree.mkdir(parents=True)
    existing = claim(f"worktree={WT}") + "\n"
    row = terminal(f"worktree=removed:{WT}")
    refusal = append_refusal(existing, row)
    assert refusal is not None
    assert "still exists" in refusal
    tree.rmdir()
    assert append_refusal(existing, row) is None


def test_disposition_pinned_removed_is_refused_while_the_path_exists_and_accepted_after_removal(
    registry: Path,
) -> None:
    tree = registry / "omni_worktrees" / "OMN-20148" / "repo"
    tree.mkdir(parents=True)
    existing = claim(f"worktree={WT}") + "\n"
    row = terminal(
        f"worktree=pinned-removed:{WT}@${REGISTRY_ENV}/.onex_state/worktree-pins/a/b"
    )
    refusal = append_refusal(existing, row)
    assert refusal is not None
    assert "still exists" in refusal
    tree.rmdir()
    assert append_refusal(existing, row) is None


def test_disposition_relative_path_is_read_from_the_registry_root(
    registry: Path,
) -> None:
    tree = registry / "omni_worktrees" / "OMN-20148" / "repo"
    tree.mkdir(parents=True)
    relative = "omni_worktrees/OMN-20148/repo"
    existing = claim(f"worktree={relative}") + "\n"
    refusal = append_refusal(existing, terminal(f"worktree=removed:{relative}"))
    assert refusal is not None
    assert "still exists" in refusal


def test_disposition_removed_cannot_be_checked_without_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    existing = claim(f"worktree={WT}") + "\n"
    refusal = append_refusal(existing, terminal(f"worktree=removed:{WT}"))
    assert refusal is not None
    assert f"{REGISTRY_ENV} is not set" in refusal


def test_disposition_kept_needs_a_reason_and_is_accepted_while_the_path_exists(
    registry: Path,
) -> None:
    tree = registry / "omni_worktrees" / "OMN-20148" / "repo"
    tree.mkdir(parents=True)
    existing = claim(f"worktree={WT}") + "\n"
    assert (
        append_refusal(
            existing, terminal(f"worktree=kept:{WT}:open-pr-awaiting-review")
        )
        is None
    )
    bare = append_refusal(existing, terminal(f"worktree=kept:{WT}"))
    assert bare is not None
    assert "reason" in bare


def test_disposition_none_is_refused_when_the_closed_claim_named_a_path(
    registry: Path,
) -> None:
    existing = claim(f"worktree={WT}") + "\n"
    refusal = append_refusal(existing, terminal("worktree=none"))
    assert refusal is not None
    assert "worktree=none" in refusal
    assert WT in refusal


def test_disposition_must_name_the_claimed_worktree(registry: Path) -> None:
    existing = claim(f"worktree={WT}") + "\n"
    other = f"${REGISTRY_ENV}/omni_worktrees/OMN-20148/other"
    refusal = append_refusal(existing, terminal(f"worktree=removed:{other}"))
    assert refusal is not None
    assert "not the worktree" in refusal
    absolute = f"{registry}/omni_worktrees/OMN-20148/repo"
    assert append_refusal(existing, terminal(f"worktree=removed:{absolute}")) is None


@pytest.mark.parametrize(
    "cell",
    [
        "worktree=gone",
        "worktree=removed:",
        f"worktree=pinned-removed:${REGISTRY_ENV}/a/b",
        "worktree=",
    ],
)
def test_disposition_malformed_terminal_cell_is_refused_by_shape(cell: str) -> None:
    assert g.row_refusals(terminal(cell)) != []


def test_payload_and_state_enforce_worktree_cells(registry: Path) -> None:
    refused = g.refusal_for_payload(claim(), LEDGER_NAME)
    assert refused is not None
    assert "no worktree cell" in refused
    tree = registry / "omni_worktrees" / "OMN-20148" / "repo"
    tree.mkdir(parents=True)
    claimed = claim(f"worktree={WT}")
    assert g.refusal_for_payload(claimed, LEDGER_NAME) is None
    existing = claimed + "\n"
    refused = append_refusal(existing, terminal())
    assert refused is not None
    assert "no worktree cell" in refused
    row = terminal(f"worktree=removed:{WT}")
    refused = append_refusal(existing, row)
    assert refused is not None
    assert "still exists" in refused
    tree.rmdir()
    assert append_refusal(existing, row) is None


def test_relative_removed_path_without_overlay_names_the_missing_key(
    tmp_path: Path,
) -> None:
    env = {
        REGISTRY_ENV: str(tmp_path / "registry"),
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "absent-overlays"),
    }
    relative = "omni_worktrees/example/repo"
    locations, reason = worktree.resolve(relative, env=env)
    assert locations == []
    assert reason is not None
    assert "repos.private_data_home_envs" in reason
    disposition, shape_reason = worktree.parse_disposition(f"removed:{relative}")
    assert shape_reason is None
    assert disposition is not None
    reason = worktree.disposition_refusal(disposition, relative, env=env)
    assert reason is not None
    assert "repos.private_data_home_envs" in reason


def test_relative_path_existing_only_in_private_clone_is_found(
    repos_env: dict[str, str],
) -> None:
    relative = "omni_worktrees/example/repo"
    private_tree = Path(repos_env["EXAMPLE_PRIVATE_DATA_HOME"]) / relative
    registry_tree = Path(repos_env[REGISTRY_ENV]) / relative
    private_tree.mkdir(parents=True)
    assert not registry_tree.exists()
    locations, reason = worktree.resolve(relative, env=repos_env)
    assert reason is None
    assert locations == [registry_tree, private_tree]
    assert worktree.exists(relative, env=repos_env) == (True, None)
    disposition, shape_reason = worktree.parse_disposition(f"removed:{relative}")
    assert disposition is not None
    assert shape_reason is None
    reason = worktree.disposition_refusal(disposition, relative, env=repos_env)
    assert reason is not None
    assert "still exists" in reason
    private_tree.rmdir()
    assert worktree.exists(relative, env=repos_env) == (False, None)
    assert worktree.disposition_refusal(disposition, relative, env=repos_env) is None
