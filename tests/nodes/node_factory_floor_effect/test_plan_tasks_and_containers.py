# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect node reads what the floor lacked: a plan's own tasks, an unmerged plan, lane containers, the fleet's cause."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifestTask,
    ModelFloorPlanInput,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorContainerHost,
    ModelFactoryFloorContainerSource,
    ModelFactoryFloorOverlay,
    ModelFactoryFloorPlanRef,
    ModelFactoryFloorTaskLinks,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_contract,
    load_lane_containers,
    load_plans,
    parse_heading_tasks,
    read_container_listing,
    runner_fleet_cause,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_effect/contract.yaml"
)
PLAN_TEXT = """# A plan

- **Tickets:** OMN-100 (the epic), OMN-200.

## 1. Tasks

**T1. First thing (repo_a).** Files: new thing. Mentions OMN-999 in prose only.

**T2. Second thing (repo_b#42, OMN-200).** Prose with repo_c#7 that belongs to no one.

1. **S1, now:** the list item names OMN-300.
2. **S2:** and one more.

**D1. A decision, not a task.**

**T1. A repeated id.** Keeps the first paragraph only.
"""


def test_a_plans_own_paragraphs_become_scoped_tasks() -> None:
    tasks = {t.id: t for t in parse_heading_tasks(PLAN_TEXT, {}, epic="OMN-100")}
    # D1 and the S items lose to the most common letter; only the T tasks remain.
    assert list(tasks) == ["T1", "T2"]
    assert all(task.scoped for task in tasks.values())
    assert tasks["T1"].tickets == ()  # prose mentions do not link
    assert tasks["T1"].title.startswith("T1 First thing")
    assert tasks["T2"].tickets == ("OMN-200",)
    assert tasks["T2"].prs == ("repo_b#42",)  # the lead's pull request, not the prose's


def test_list_items_read_their_whole_line_and_the_epic_belongs_to_no_task() -> None:
    text = "1. **S1, now:** tokens, OMN-100 and OMN-300.\n2. **S2:** the models.\n"
    tasks = {t.id: t for t in parse_heading_tasks(text, {}, epic="OMN-100")}
    assert list(tasks) == ["S1", "S2"]
    assert tasks["S1"].tickets == ("OMN-300",)


def test_links_add_the_tickets_a_plan_does_not_name() -> None:
    tasks = {t.id: t for t in parse_heading_tasks(PLAN_TEXT, {"T1": ("OMN-500",)})}
    assert tasks["T1"].tickets == ("OMN-500",)


def _plans_tree(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    (kb / "beta/plans").mkdir(parents=True)
    (kb / "beta/plans/2026-11-01-a-plan.md").write_text(PLAN_TEXT, encoding="utf-8")
    return kb


def _load(
    kb: Path,
    plan_refs: tuple[ModelFactoryFloorPlanRef, ...] = (),
    task_links: tuple[ModelFactoryFloorTaskLinks, ...] = (),
) -> dict[str, ModelFloorPlanInput]:
    plans = load_plans(
        kb=kb,
        plans_relpath="beta/plans",
        manifest_suffix=".tasks.yaml",
        header_keys=("ticket", "tickets", "epic"),
        contains=("a-plan", "unmerged"),
        plans_today={
            "plans": [{"path": "beta/plans/2026-11-01-a-plan.md", "tasks": []}]
        },
        plan_refs=plan_refs,
        task_links=task_links,
    )
    return {plan.plan_path: plan for plan in plans}


def tasks_of(plan: ModelFloorPlanInput) -> tuple[ModelFloorManifestTask, ...]:
    assert plan.manifest is not None
    return plan.manifest.tasks


def test_a_plan_with_an_empty_table_is_drawn_from_its_paragraphs(
    tmp_path: Path,
) -> None:
    plans = _load(
        _plans_tree(tmp_path),
        task_links=(
            ModelFactoryFloorTaskLinks(
                plan_path_contains="a-plan", tasks={"T1": ("OMN-500",)}
            ),
        ),
    )
    tasks = tasks_of(plans["beta/plans/2026-11-01-a-plan.md"])
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert tasks[0].tickets == ("OMN-500",)
    assert "OMN-100" not in tasks[1].tickets


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def test_a_plan_on_an_unmerged_ref_is_read_from_the_ref_until_it_merges(
    tmp_path: Path,
) -> None:
    kb = _plans_tree(tmp_path)
    _git(kb, "init", "-q", "-b", "main")
    _git(kb, "add", "-A")
    _git(kb, "commit", "-q", "-m", "base")
    _git(kb, "checkout", "-q", "-b", "draft")
    draft = kb / "beta/plans/2026-11-01-unmerged.md"
    draft.write_text("# Unmerged\n\n**T1. Only here (repo#1).**\n", encoding="utf-8")
    manifest = draft.with_name("2026-11-01-unmerged.tasks.yaml")
    manifest.write_text(
        yaml.safe_dump({"title": "Unmerged (tasks)", "tasks": [{"id": "W1"}]}),
        encoding="utf-8",
    )
    _git(kb, "add", "-A")
    _git(kb, "commit", "-q", "-m", "draft plan")
    _git(kb, "checkout", "-q", "main")
    assert not draft.exists()

    ref = ModelFactoryFloorPlanRef(
        path="beta/plans/2026-11-01-unmerged.md", ref="draft"
    )
    plans = _load(kb, plan_refs=(ref,))
    drawn = plans["beta/plans/2026-11-01-unmerged.md"]
    assert [t.id for t in tasks_of(drawn)] == ["W1"]

    # Once the plan is on the branch the clone is on, the clone's file wins and the ref is not read.
    _git(kb, "merge", "-q", "draft")
    absent = ModelFactoryFloorPlanRef(
        path="beta/plans/2026-11-01-unmerged.md", ref="no-such-ref"
    )
    assert "beta/plans/2026-11-01-unmerged.md" in _load(kb, plan_refs=(absent,))

    # A ref that does not exist draws nothing, so the declared line stays "plan file not found".
    missing = ModelFactoryFloorPlanRef(
        path="beta/plans/2026-11-01-unmerged-x.md", ref="nope"
    )
    assert "beta/plans/2026-11-01-unmerged-x.md" not in _load(kb, plan_refs=(missing,))


def _container_source() -> ModelFactoryFloorContainerSource:
    return load_contract(CONTRACT).runtime.container_source


def _declarations() -> tuple[ModelLaneContainerBudgetDeclaration, ...]:
    return (
        ModelLaneContainerBudgetDeclaration(
            host="host-a",
            lane="dev",
            compose_project="proj",
            application_container_budget=1,
            infrastructure_markers=("postgres",),
        ),
    )


LISTING = "\n".join(
    json.dumps(
        {"Names": name, "State": "running", "Labels": "com.docker.compose.project=proj"}
    )
    for name in ("proj-runtime-1", "proj-postgres-1", "proj-extra-1")
)


def test_each_declared_host_is_read_and_judged_against_its_budget() -> None:
    verdicts = load_lane_containers(
        source=_container_source(),
        hosts=(
            ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
        ),
        declarations=_declarations(),
        read=lambda _source, _host: (LISTING, ""),
    )
    assert verdicts is not None
    (lane,) = verdicts[0].lanes
    assert (lane.status, lane.observed, lane.budget) == ("FAIL", 2, 1)
    assert lane.containers == ("proj-extra-1", "proj-runtime-1")


GHOST = json.dumps({"Names": "", "State": "dead", "Labels": ""})


def _read_with_stdout(monkeypatch: pytest.MonkeyPatch, stdout: str) -> tuple[str, str]:
    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return read_container_listing(
        _container_source(),
        ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
    )


def test_a_dead_row_with_no_name_is_dropped_so_it_cannot_refuse_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = LISTING.splitlines()
    listing, error = _read_with_stdout(
        monkeypatch, "\n".join([rows[0], GHOST, rows[1], GHOST, rows[2]])
    )
    assert error == ""
    assert GHOST not in listing.splitlines()
    assert len(listing.splitlines()) == 3
    verdicts = load_lane_containers(
        source=_container_source(),
        hosts=(
            ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
        ),
        declarations=_declarations(),
        read=lambda _source, _host: (listing, error),
    )
    assert verdicts is not None
    (lane,) = verdicts[0].lanes
    assert (lane.status, lane.observed, lane.budget) == ("FAIL", 2, 1)

    # Without the drop the budget node refuses the whole listing and the lane reads UNKNOWN.
    refused = load_lane_containers(
        source=_container_source(),
        hosts=(
            ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
        ),
        declarations=_declarations(),
        read=lambda _source, _host: ("\n".join([rows[0], GHOST]), ""),
    )
    assert refused is not None
    assert refused[0].lanes[0].status == "UNKNOWN"
    assert "names no container" in refused[0].lanes[0].detail


def test_a_nameless_row_in_any_other_state_still_refuses_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nameless_running = json.dumps({"Names": "", "State": "running", "Labels": ""})
    listing, _ = _read_with_stdout(
        monkeypatch, "\n".join([LISTING.splitlines()[0], nameless_running])
    )
    assert nameless_running in listing.splitlines()
    verdicts = load_lane_containers(
        source=_container_source(),
        hosts=(
            ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
        ),
        declarations=_declarations(),
        read=lambda _source, _host: (listing, ""),
    )
    assert verdicts is not None
    assert verdicts[0].lanes[0].status == "UNKNOWN"


def test_a_dead_row_that_has_a_name_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    named_dead = json.dumps(
        {
            "Names": "proj-old-1",
            "State": "dead",
            "Labels": "com.docker.compose.project=proj",
        }
    )
    listing, _ = _read_with_stdout(monkeypatch, named_dead)
    assert named_dead in listing.splitlines()


def test_a_host_that_cannot_be_read_is_unknown_with_its_cause_not_zero() -> None:
    verdicts = load_lane_containers(
        source=_container_source(),
        hosts=(
            ModelFactoryFloorContainerHost(host="host-a", ssh_target="user@host-a"),
        ),
        declarations=_declarations(),
        read=lambda _source, _host: (
            "",
            "the listing on host-a exited 255: Permission denied",
        ),
    )
    assert verdicts is not None
    (lane,) = verdicts[0].lanes
    assert lane.status == "UNKNOWN"
    assert lane.observed is None
    assert "Permission denied" in lane.detail


def test_no_declared_host_means_the_containers_are_not_read() -> None:
    assert (
        load_lane_containers(
            source=_container_source(), hosts=(), declarations=_declarations()
        )
        is None
    )


def test_the_listing_command_names_its_fields_and_the_overlay_validates() -> None:
    source = _container_source()
    assert "{{json .}}" not in source.listing_command
    assert "BatchMode=yes" in source.ssh_options
    overlay = ModelFactoryFloorOverlay.model_validate(
        {
            "plan_path_contains": ["x"],
            "container_read_line": "x",
            "container_hosts": [{"host": "h", "ssh_target": "u@h"}],
            "container_budgets": [
                {
                    "host": "h",
                    "lane": "dev",
                    "compose_project": "p",
                    "application_container_budget": 3,
                }
            ],
        }
    )
    assert overlay.container_budgets[0].application_container_budget == 3


def test_an_unread_runner_fleet_names_the_file_and_why(tmp_path: Path) -> None:
    missing = runner_fleet_cause(tmp_path / "runner-fleet.json")
    assert "runner-fleet.json does not exist" in missing
    broken = tmp_path / "runner-fleet.json"
    broken.write_text("{}", encoding="utf-8")
    assert "cannot be read" in runner_fleet_cause(broken)
    broken.write_text(
        json.dumps(
            {
                "observed_at": "2026-11-01T22:00:00Z",
                "runners": [{"host": "host-101", "status": "online"}],
            }
        ),
        encoding="utf-8",
    )
    assert runner_fleet_cause(broken) == ""
