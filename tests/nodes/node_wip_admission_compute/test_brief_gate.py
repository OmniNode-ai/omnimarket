# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane runner's seam: new work waits at the limit, a fix lane and a host with no overlay do not.

Every repository name, lane and ticket id below is an invented fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import rules_wip_admission as rules
from omnimarket.handlers.wip_admission_brief_gate import dispatch_wip_refusal
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    CONFIG,
    NOW,
    REPO_A,
    overlay_doc,
    pr,
    watcher,
)

pytestmark = pytest.mark.unit


def host(tmp_path: Path, *, prs: int, ledger: str = "") -> dict[str, str]:
    """A launching host: the overlay, a watcher state file with ``prs`` open PRs and a ledger."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "watcher.json"
    state.write_text(
        json.dumps(watcher(*(pr(REPO_A, i) for i in range(1, prs + 1)))),
        encoding="utf-8",
    )
    doc = {**overlay_doc(), "watcher_state_path": str(state)}
    folder = tmp_path / "overlay" / "wip_admission"
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    ledger_path = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger_path.write_text(ledger, encoding="utf-8")
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlay"),
        "ONEX_LEDGER_PATH": str(ledger_path),
    }


def ask(env: dict[str, str], **extra: Any) -> str | None:
    args: dict[str, Any] = {
        "lane": "fixture-lane",
        "repo": REPO_A,
        "ticket": "OMN-5",
        "prs": (),
        "environ": env,
        "now": NOW,
    }
    return dispatch_wip_refusal(**{**args, **extra})


def test_new_work_is_refused_at_the_limit_and_admitted_under_it(tmp_path: Path) -> None:
    full = host(tmp_path / "full", prs=11)
    why = ask(full)
    assert why is not None
    assert why.startswith("REMOTE-LANE-WIP-REFUSED lane=fixture-lane REFUSE ")
    assert ask(host(tmp_path / "empty", prs=0)) is None


def test_a_lane_working_a_named_pull_request_is_never_refused(tmp_path: Path) -> None:
    full = host(tmp_path, prs=11)
    assert ask(full, prs=(f"fixture-org/{REPO_A}#1",)) is None


def test_a_lane_named_for_a_fix_is_never_refused(tmp_path: Path) -> None:
    assert ask(host(tmp_path, prs=11), lane="fixture-cause-core-0000") is None


def test_the_watcher_state_path_is_read_with_the_environment_substituted(
    tmp_path: Path,
) -> None:
    env = host(tmp_path / "full", prs=11)
    overlay = tmp_path / "full" / "overlay" / "wip_admission" / "overlay.yaml"
    doc = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    doc["watcher_state_path"] = "${FIXTURE_ROOT}/watcher.json"
    overlay.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert ask({**env, "FIXTURE_ROOT": str(tmp_path / "full")}) is not None
    unset = ask(env)
    assert unset is not None
    assert "watcher state unread: KeyError" in unset


def test_a_host_with_no_overlay_judges_nothing(tmp_path: Path) -> None:
    assert ask({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")}) is None


def test_an_unread_state_refuses_new_work_naming_why_and_admits_a_fix(
    tmp_path: Path,
) -> None:
    env = host(tmp_path, prs=0)
    Path(
        yaml.safe_load((tmp_path / "overlay/wip_admission/overlay.yaml").read_text())[
            "watcher_state_path"
        ]
    ).unlink()
    why = ask(env)
    assert why is not None
    assert "watcher state unread" in why
    assert ask(env, prs=(f"{REPO_A}#1",)) is None


def test_live_claims_from_the_ledger_count_beside_the_pull_requests(
    tmp_path: Path,
) -> None:
    rows = "".join(
        f"2026-10-30T11:{i:02d}:00Z | CLAIM | lane=fixture-l{i} | ticket=OMN-{i} | "
        f"worktree=omni_worktrees/OMN-{i}/{REPO_A} | est\n"
        for i in range(1, 12)
    )
    assert ask(host(tmp_path, prs=0, ledger=rows)) is not None


def test_a_claim_ends_with_its_terminal_and_with_a_quiet_lane() -> None:
    open_row = "2026-10-30T11:00:00Z | CLAIM | lane=l1 | ticket=OMN-1 | repo=fixture-repo-a | x\n"
    closed = open_row + "2026-10-30T11:30:00Z | TERMINAL | lane=l1 | outcome=done\n"
    found = rules.claims_from_ledger(open_row, NOW, CONFIG)
    assert [(c.lane, c.ticket, c.repo) for c in found] == [
        ("l1", "OMN-1", "fixture-repo-a")
    ]
    assert rules.claims_from_ledger(closed, NOW, CONFIG) == []
    quiet = "2026-10-29T01:00:00Z | CLAIM | lane=l2 | ticket=OMN-2 | x\n"
    assert rules.claims_from_ledger(quiet, NOW, CONFIG) == []


def test_the_command_line_prints_the_refusal_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_wip_admission_compute import runtime_brief_gate as gate

    for key, value in host(tmp_path, prs=11).items():
        monkeypatch.setenv(key, value)
    assert gate.main(["--lane", "fixture-lane", "--repo", REPO_A]) == gate.EXIT_REFUSE
    assert capsys.readouterr().out.startswith("REMOTE-LANE-WIP-REFUSED ")
    assert (
        gate.main(["--lane", "fixture-lane", "--repo", REPO_A, "--pr", f"{REPO_A}#1"])
        == gate.EXIT_ADMIT
    )
