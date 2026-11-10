# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the delegation tracker tests: a runtime, a registry home with the four sources, and a fake command runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    load_runtime,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationInputs,
    ModelDelegationLane,
    ModelDelegationOverlay,
    ModelDelegationRuntime,
    ModelMirrorTicket,
    ModelScenarioRead,
    ModelScenarioReads,
    ModelScenarioRun,
    ModelSourceReading,
    ModelWatcherPr,
    PrCi,
    PrState,
    ScenarioReadKind,
)

NOW = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
OVERLAY_FILE = Path(__file__).parent / "fixtures" / "overlay.yaml"
OVERLAY = ModelDelegationOverlay.model_validate(
    yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
)
RT = load_runtime(overlay=OVERLAY)
LAB_NODE_OVERLAY_FILE = Path(__file__).parent / "fixtures" / "overlay_lab_node.yaml"
LAB_NODE_RT = load_runtime(
    overlay=ModelDelegationOverlay.model_validate(
        yaml.safe_load(LAB_NODE_OVERLAY_FILE.read_text(encoding="utf-8"))
    )
)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conclusion: str, minutes_ago: int, status: str = "completed"
) -> ModelScenarioRun:
    at = iso(NOW - timedelta(minutes=minutes_ago))
    return ModelScenarioRun(
        run_id=minutes_ago,
        status=status,
        conclusion=conclusion,
        event="schedule",
        created_at=at,
        updated_at=at,
        url=f"https://github.com/example/actions/runs/{minutes_ago}",
    )


def read(
    scenario: str,
    *runs: ModelScenarioRun,
    kind: ScenarioReadKind = "read",
    read_minutes_ago: int = 1,
    reason: str = "",
) -> ModelScenarioRead:
    return ModelScenarioRead(
        scenario=scenario,
        read=kind,
        read_at=iso(NOW - timedelta(minutes=read_minutes_ago)),
        runs=runs,
        reason=reason,
    )


def all_reads(conclusion: str = "success") -> tuple[ModelScenarioRead, ...]:
    return tuple(read(d.id, run(conclusion, 10)) for d in RT.scenarios)


def mirror(
    number: int,
    *,
    status_type: str = "backlog",
    start_when: str = "",
    plan_task: str = "A1",
) -> ModelMirrorTicket:
    return ModelMirrorTicket(
        id=f"OMN-{number}",
        title=f"Title of {number}",
        plan_task=plan_task,
        linear_status="Done" if status_type == "completed" else "Backlog",
        status_type=status_type,
        start_when=start_when,
        read_at=iso(NOW - timedelta(hours=3)),
    )


def pr(
    ticket: int,
    state: PrState = "open",
    ci: PrCi = "green",
    repo: str = "omnimarket",
    number: int = 100,
) -> ModelWatcherPr:
    return ModelWatcherPr(
        ref=f"{repo}#{number}",
        repo=repo,
        number=number,
        ticket=f"OMN-{ticket}",
        title=f"feat(OMN-{ticket}): a change",
        state=state,
        ci=ci,
        ci_note="red: CI Summary" if ci == "red" else "",
        url=f"https://github.com/example/{repo}/pull/{number}",
    )


def lane(
    ticket: int, host: str = "h053", liveness: str = "alive"
) -> ModelDelegationLane:
    return ModelDelegationLane(
        lane=f"lane-{ticket}", ticket=f"OMN-{ticket}", host=host, liveness=liveness
    )


def sources(
    *, watcher: bool = True, record: bool = True
) -> tuple[ModelSourceReading, ...]:
    return (
        ModelSourceReading(id="mirror", title="Ticket mirror", ok=True),
        ModelSourceReading(id="watcher", title="Pull request watcher", ok=watcher),
        ModelSourceReading(id="record", title="Factory floor record", ok=record),
        ModelSourceReading(
            id="scenario_reads", title="Scenario workflow runs", ok=True
        ),
    )


def inputs(
    *,
    reads: tuple[ModelScenarioRead, ...] | None = None,
    mirrors: tuple[ModelMirrorTicket, ...] = (),
    prs: tuple[ModelWatcherPr, ...] = (),
    lanes: tuple[ModelDelegationLane, ...] = (),
    watcher: bool = True,
    record: bool = True,
) -> ModelDelegationInputs:
    return ModelDelegationInputs(
        scenario_reads=None
        if reads is None
        else ModelScenarioReads(updated_at=iso(NOW), reads=reads),
        mirror=mirrors,
        prs=prs,
        lanes=lanes,
        sources=sources(watcher=watcher, record=record),
    )


def runtime(**changes: object) -> ModelDelegationRuntime:
    return RT.model_copy(update=changes)


def runs_answer(*runs: tuple[str, str, int]) -> str:
    """The one JSON line the source-control read prints: (status, conclusion, minutes ago) per run."""
    return "bus-binding lane=dev\n" + json.dumps(
        {
            "verb": "run-list",
            "outcome": "completed",
            "result": {
                "runs": [
                    {
                        "id": 1000 + i,
                        "status": status,
                        "conclusion": conclusion,
                        "event": "schedule",
                        "created_at": iso(NOW - timedelta(minutes=ago)),
                        "updated_at": iso(NOW - timedelta(minutes=ago)),
                        "html_url": f"https://github.com/example/actions/runs/{1000 + i}",
                    }
                    for i, (status, conclusion, ago) in enumerate(runs)
                ]
            },
        }
    )


def missing_workflow_answer() -> str:
    return json.dumps(
        {
            "verb": "run-list",
            "outcome": "failed",
            "result": {
                "status": "failed",
                "failure_reason": "GitHub GET /repos/x/y/actions/workflows/z/runs answered HTTP 404: Not Found",
            },
        }
    )


class FakeRunner:
    """Answers each workflow's read from a table; records the order the workflows were read in."""

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.read: list[str] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        if "--workflow" not in argv:
            return 1, "", "not a scenario read"
        workflow = argv[argv.index("--workflow") + 1]
        self.read.append(workflow)
        return self.answers.get(workflow, (1, "", "no answer planted"))


def write_home(
    tmp: Path,
    *,
    tickets: dict[int, dict[str, str]] | None = None,
    prs: dict[str, dict[str, object]] | None = None,
    lanes: list[dict[str, object]] | None = None,
    watcher_tick: datetime = NOW,
    record_at: datetime = NOW,
) -> Path:
    """A registry home holding a ticket mirror, a watcher state and a floor record."""
    home = tmp / "home"
    mirror_dir = home / RT.ticket_mirror_relpath
    mirror_dir.mkdir(parents=True)
    for number in range(RT.plan_ticket_first, RT.plan_ticket_last + 1):
        spec = (tickets or {}).get(number, {})
        description = "Plan task: beta/plans/x.md A1\n\n"
        if spec.get("start_when"):
            description += f"Start when: {spec['start_when']}\n"
        (mirror_dir / f"OMN-{number}.json").write_text(
            json.dumps(
                {
                    "read_at": iso(NOW - timedelta(hours=2)),
                    "issues": [
                        {
                            "id": f"OMN-{number}",
                            "title": f"Title of {number}",
                            "status": spec.get("status", "Backlog"),
                            "statusType": spec.get("status_type", "backlog"),
                            "description": description,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    watcher = home / RT.watcher_relpath
    watcher.parent.mkdir(parents=True)
    watcher.write_text(
        json.dumps({"last_tick": iso(watcher_tick), "prs": prs or {}}), encoding="utf-8"
    )
    (home / RT.record_relpath).parent.mkdir(parents=True, exist_ok=True)
    (home / RT.record_relpath).write_text(
        json.dumps({"generated_at": iso(record_at), "floor": {"lanes": lanes or []}}),
        encoding="utf-8",
    )
    return home


def watcher_pr(
    ticket: int,
    *,
    state: str = "OPEN",
    verdict: str | None = "GREEN",
    number: int = 100,
    repo: str = "omnimarket",
    title: str | None = None,
    head_ref: str = "",
) -> dict[str, object]:
    return {
        "facts": {
            "title": title if title is not None else f"feat(OMN-{ticket}): a change",
            "head_ref": head_ref,
            "state": state,
            "number": number,
            "repo": repo,
        },
        "ci": {"verdict": verdict, "read_at": iso(NOW)} if verdict else None,
        "reason": "red: CI Summary" if verdict == "RED" else "",
    }
