# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A plan's stale PR list must not turn an unrelated dependency update into its stop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorTableTask,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_watcher
from tests.nodes.node_factory_floor_compute.support import NOW, request

pytestmark = pytest.mark.unit

PR = "omnimemory#713"
TICKET = "OMN-19998"


def _plan(*, manifest: bool) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path="beta/plans/2026-10-28-validators-to-core-and-hardcoded-rules.md",
        tickets=(TICKET,),
        manifest=ModelFloorManifest(
            tasks=(ModelFloorManifestTask(id="T1", tickets=(TICKET,), prs=(PR,)),)
        )
        if manifest
        else None,
        table_tasks=()
        if manifest
        else (
            ModelFloorTableTask(
                label="T1", tickets=(TICKET,), prs=(PR,), state="open-red"
            ),
        ),
    )


@pytest.mark.parametrize("manifest", [False, True], ids=["table", "manifest"])
@pytest.mark.parametrize(
    ("title", "head_ref", "belongs"),
    [
        (
            "build(deps): bump the actions group with 6 updates",
            "dependabot/github_actions/actions-e4afbbc559",
            False,
        ),
        ("fix(OMN-1): unrelated work", "omn-1-another-task", False),
        ("fix(OMN-19998): own work", "omn-19998-own-work", True),
        ("fix: own work", "omn-19998-own-work", True),
        ("", "", True),
    ],
    ids=[
        "recorded-dependency-update",
        "other-ticket",
        "own-ticket",
        "own-branch",
        "identity-unread",
    ],
)
def test_only_owned_red_prs_stop_a_line(
    tmp_path: Path, manifest: bool, title: str, head_ref: str, belongs: bool
) -> None:
    # Reproduce the watcher's deployed shape through its source adapter. A dependency
    # update's changelog may mention our ticket without making the update our work.
    path = tmp_path / "watcher.json"
    path.write_text(
        json.dumps(
            {
                "last_tick": NOW.isoformat(),
                "prs": {
                    PR: {
                        "facts": {
                            "state": "OPEN",
                            "title": title,
                            "head_ref": head_ref,
                            "body": f"Changelog includes a commit for {TICKET}",
                        },
                        "cls": "red",
                        "ci": {
                            "verdict": "RED",
                            "red": ["public-repo-hygiene / public-repo-hygiene"],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    watcher = load_watcher(path, NOW, max_age_min=8)
    facts = request(plans=(_plan(manifest=manifest),)).model_copy(
        update={"watcher": watcher}
    )
    result = HandlerFactoryFloor().handle(facts)
    line = result.lines[0]
    item = line.stations[0].items[0]

    assert item.prs == ((PR,) if belongs else ())
    assert (line.condition == "stopped") is belongs
    if belongs:
        assert line.blocker is not None
        assert f"PR {PR} CI red" in line.blocker.reason
        assert PR in line.blocker.evidence
    else:
        assert line.blocker is None
        if not manifest:
            assert item.state == "wait"
            assert "no matching pull request" in item.note


def test_an_unread_listed_pr_is_retained() -> None:
    # An absent record cannot disprove an explicit cross-repository link.
    result = HandlerFactoryFloor().handle(request(plans=(_plan(manifest=False),)))
    assert result.lines[0].stations[0].items[0].prs == (PR,)


def test_table_infers_the_owned_red_pr_instead_of_its_stale_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watcher.json"
    owned = "omnimarket#1"
    path.write_text(
        json.dumps(
            {
                "last_tick": NOW.isoformat(),
                "prs": {
                    owned: {
                        "facts": {"state": "OPEN", "title": f"fix({TICKET}): own work"},
                        "ci": {"verdict": "RED"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    facts = request(plans=(_plan(manifest=False),)).model_copy(
        update={"watcher": load_watcher(path, NOW, max_age_min=8)}
    )
    line = HandlerFactoryFloor().handle(facts).lines[0]
    assert line.stations[0].items[0].prs == (owned, PR)
    assert line.condition == "stopped"
    assert line.blocker is not None
    assert owned in line.blocker.reason
