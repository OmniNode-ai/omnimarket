# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A plan only on an open pull request's head is drawn from that head, through the local clone's refs (OMN-21930)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_factory_floor_input import (
    ModelFloorPr,
    ModelFloorWatcher,
    PrState,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.draft_plan_refs import (
    draft_plan_refs,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.plan_task_tickets import (
    prose_task_tickets,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorPlanRef,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_plans

pytestmark = pytest.mark.unit

PLANS = "beta/plans"
DRAFT_PLAN = "beta/plans/2026-10-31-draft-wave-plan.md"
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone_with_draft_plan(root: Path) -> Path:
    """A plans clone whose default branch holds one plan and whose ``origin/plan/orch`` ref holds another."""
    kb = root / "plans_repo"
    (kb / PLANS).mkdir(parents=True)
    (kb / PLANS / "2026-10-01-on-main.md").write_text("# On main\n", encoding="utf-8")
    _git(kb, "init", "-q", "-b", "main")
    _git(kb, "add", "-A")
    _git(kb, "commit", "-q", "-m", "base")
    _git(kb, "checkout", "-q", "-b", "plan/orch")
    draft = kb / DRAFT_PLAN
    draft.write_text("# Orchestration alignment\n", encoding="utf-8")
    draft.with_name(draft.stem + ".tasks.yaml").write_text(
        yaml.safe_dump(
            {"title": "Orchestration (tasks)", "tasks": [{"id": "W0.1"}, {"id": "G0"}]}
        ),
        encoding="utf-8",
    )
    _git(kb, "add", "-A")
    _git(kb, "commit", "-q", "-m", "draft plan")
    # The canonical clone keeps a pull request's head as a remote ref; the lane never fetches it.
    _git(kb, "update-ref", "refs/remotes/origin/plan/orch", "plan/orch")
    _git(kb, "checkout", "-q", "main")
    assert not (kb / DRAFT_PLAN).exists()
    return kb


def _watcher(*prs: ModelFloorPr) -> ModelFloorWatcher:
    return ModelFloorWatcher(last_tick=NOW, prs=prs)


def _pr(key: str, state: PrState, head_ref: str) -> ModelFloorPr:
    return ModelFloorPr(key=key, state=state, draft=True, head_ref=head_ref)


def _drawn(
    kb: Path, refs: tuple[ModelFactoryFloorPlanRef, ...]
) -> dict[str, list[str]]:
    plans = load_plans(
        kb=kb,
        plans_relpath=PLANS,
        manifest_suffix=".tasks.yaml",
        header_keys=("ticket",),
        contains=("draft-wave-plan",),
        plans_today=None,
        plan_refs=refs,
    )
    return {
        plan.plan_path: [t.id for t in (plan.manifest.tasks if plan.manifest else ())]
        for plan in plans
    }


def test_draft_pr_plan_is_read_from_the_open_pull_requests_head_ref(
    tmp_path: Path,
) -> None:
    kb = _clone_with_draft_plan(tmp_path)
    # Positive control: with no ref the plan is on no branch the clone has checked out, so no line is drawn.
    assert _drawn(kb, ()) == {}
    refs = draft_plan_refs(
        kb=kb,
        plans_relpath=PLANS,
        contains=("draft-wave-plan",),
        watcher=_watcher(_pr("plans_repo#1572", "OPEN", "plan/orch")),
        declared=(),
    )
    assert refs == (ModelFactoryFloorPlanRef(path=DRAFT_PLAN, ref="origin/plan/orch"),)
    assert _drawn(kb, refs) == {DRAFT_PLAN: ["W0.1", "G0"]}


def test_draft_pr_plan_ignores_a_closed_pull_request_another_repository_and_a_missing_ref(
    tmp_path: Path,
) -> None:
    kb = _clone_with_draft_plan(tmp_path)
    watcher = _watcher(
        _pr("plans_repo#1500", "CLOSED", "plan/orch"),
        _pr("omnimarket#1572", "OPEN", "plan/orch"),
        _pr("plans_repo#1573", "OPEN", "plan/not-in-the-clone"),
    )
    assert (
        draft_plan_refs(
            kb=kb,
            plans_relpath=PLANS,
            contains=("draft-wave-plan",),
            watcher=watcher,
            declared=(),
        )
        == ()
    )


def test_draft_pr_plan_leaves_a_declared_ref_and_a_plan_on_the_default_branch_alone(
    tmp_path: Path,
) -> None:
    kb = _clone_with_draft_plan(tmp_path)
    watcher = _watcher(_pr("plans_repo#1572", "OPEN", "plan/orch"))
    declared = (ModelFactoryFloorPlanRef(path=DRAFT_PLAN, ref="origin/elsewhere"),)
    assert (
        draft_plan_refs(
            kb=kb,
            plans_relpath=PLANS,
            contains=("draft-wave-plan",),
            watcher=watcher,
            declared=declared,
        )
        == declared
    )
    on_main = draft_plan_refs(
        kb=kb,
        plans_relpath=PLANS,
        contains=("on-main",),
        watcher=watcher,
        declared=(),
    )
    assert on_main == ()


def test_draft_pr_plan_a_stale_declared_ref_does_not_hide_the_pull_request_head(
    tmp_path: Path,
) -> None:
    kb = _clone_with_draft_plan(tmp_path)
    refs = (
        ModelFactoryFloorPlanRef(path=DRAFT_PLAN, ref="origin/renamed-away"),
        ModelFactoryFloorPlanRef(path=DRAFT_PLAN, ref="origin/plan/orch"),
    )
    assert _drawn(kb, refs) == {DRAFT_PLAN: ["W0.1", "G0"]}


def test_plan_prose_pairs_a_task_id_with_its_tickets() -> None:
    text = (
        "- **Ticket:** OMN-21433 (the epic). Minted: T0.1 OMN-21494, T0.2 OMN-21495, "
        "T2.1 OMN-21500 and OMN-21501, T2.6 OMN-21496 to OMN-21499.\n"
        "- **T1.1 + K2 (OMN-21515).** Needs T0.1, OMN-21494 first.\n"
    )
    assert prose_task_tickets(text, "OMN-21433") == {
        "T0.1": ("OMN-21494",),
        "T0.2": ("OMN-21495",),
        "T2.1": ("OMN-21500", "OMN-21501"),
        "T2.6": ("OMN-21496", "OMN-21497", "OMN-21498", "OMN-21499"),
        "T1.1": ("OMN-21515",),
    }


def test_plan_prose_links_reach_the_tasks_a_plan_writes_without_tickets(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "plans_repo"
    (kb / PLANS).mkdir(parents=True)
    (kb / PLANS / "2026-10-28-validators.md").write_text(
        "# Validators\n\n- **Ticket:** OMN-21433 (the epic). Minted: T0.1 OMN-21494, T0.2 OMN-21495.\n\n"
        "## C. Plan\n\n**T0.1 Private vocabulary (UPDATE).**\n\n**T0.2 Pin parity (UPDATE).**\n",
        encoding="utf-8",
    )
    (plan,) = load_plans(
        kb=kb,
        plans_relpath=PLANS,
        manifest_suffix=".tasks.yaml",
        header_keys=("ticket",),
        contains=("validators",),
        plans_today=None,
    )
    assert plan.manifest is not None
    assert {t.id: t.tickets for t in plan.manifest.tasks} == {
        "T0.1": ("OMN-21494",),
        "T0.2": ("OMN-21495",),
    }


def test_draft_pr_plan_is_drawn_by_the_effect_handler_from_the_watcher_and_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole read: the overlay declares the plan, no ref names it, the watcher holds the open pull request."""
    kb = _clone_with_draft_plan(tmp_path)
    home = tmp_path / "home"
    (home / ".claude_scratch/pr-watcher").mkdir(parents=True)
    (home / ".claude_scratch/pr-watcher/state.json").write_text(
        json.dumps(
            {
                "last_tick": "2026-11-02T11:59:00Z",
                "prs": {
                    "plans_repo#1572": {
                        "facts": {
                            "state": "OPEN",
                            "draft": True,
                            "title": "docs(plan): a draft wave plan",
                            "head_ref": "plan/orch",
                        },
                        "cls": "held-excluded",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"plan_path_contains": ["draft-wave-plan"]}), encoding="utf-8"
    )
    rows = tmp_path / "ledger.txt"
    rows.write_text("", encoding="utf-8")
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_OVERLAY", str(overlay))
    monkeypatch.setenv("ONEX_REMOTE_LANE_DIR", str(tmp_path / "remote-lanes"))
    monkeypatch.setenv("LANDING_PLACEMENT_DIR", str(tmp_path / "placement"))
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(tmp_path / "no-such-hosts.yaml"))
    result = HandlerFactoryFloorEffect().handle(
        ModelFactoryFloorRunRequest(
            home=str(home),
            kb=str(kb),
            ledger_rows_file=str(rows),
            now=datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC),
        )
    )
    document = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    (line,) = document["lines"]
    assert line["line_id"] == "2026-10-31-draft-wave-plan"
    assert line["source"] == "manifest"
    assert [item["id"] for st in line["stations"] for item in st["items"]] == [
        "W0.1",
        "G0",
    ]
