# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stored work is rebuilt at the tick boundary before any selection or dispatch."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import (
    ModelBacklogSources,
    ModelLabFillRequest,
)

pytestmark = pytest.mark.unit
NOW = "2026-11-02T16:00:00Z"


def watcher() -> dict:
    def pr(state: str, verdict: str = "RED") -> dict:
        return {
            "facts": {
                "state": state,
                "repo": "omnimarket",
                "title": "Repair factory checks",
                "head_sha": "a" * 40,
                "updated_at": NOW,
            },
            "ci": {"verdict": verdict, "sha": "a" * 40},
            "cls": "red",
        }

    return {
        "schema": 1,
        "last_tick": NOW,
        "last_full_resync": NOW,
        "repos": {"omnimarket": {}},
        "prs": {
            "omnimarket#1": pr("MERGED"),
            "omnimarket#2": pr("CLOSED"),
            "omnimarket#4": pr("OPEN"),
            "omnimarket#5": pr("OPEN", "GREEN"),
        },
    }


def run_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_only: bool = False,
    hosts: list | None = None,
    default_binding: bool = False,
    recorder: Callable[[str, str, str], str | None] | None = None,
) -> tuple[Path, object]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    backlog = state / fill.BACKLOG_FILE
    backlog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": f"omnimarket#{n}",
                        "kind": "process-fix",
                        "repo": "omnimarket",
                        "pr": f"omnimarket#{n}",
                        "fact_at": "2026-11-01T18:45:00Z",
                    }
                    for n in (1, 2, 3, 4)
                ],
                "standing_tickets": {"process-fix": "OMN-22147"},
            }
        )
    )
    snapshot = tmp_path / "tickets.json"
    snapshot.write_text(json.dumps({"captured_at": NOW, "issues": []}))
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    monkeypatch.setenv("ONEX_PR_CLAIMS_DIR", str(tmp_path / "claims"))
    (tmp_path / "claims").mkdir(exist_ok=True)
    sources = ModelBacklogSources(
        linear_snapshot=snapshot, ledger_tickets=lambda: (set(), {})
    )
    if default_binding:
        monkeypatch.setattr(fill, "live_sources", lambda _now, _env: sources)
    result = fill.HandlerLabFill().handle(
        ModelLabFillRequest(
            now=NOW,
            request={
                "watcher_state": watcher(),
                "lab_headroom": {"hosts": hosts or []},
            },
            lines=["MISSING lab-headroom:host-a | free Claude slots 1"]
            if hosts
            else [],
            launch=None,
            state_dir=state,
            backlog=None,
            runner=tmp_path / "unused",
            sources=None if default_binding else sources,
            out_of_scope={},
            read_only=read_only,
            record=recorder,
        )
    )
    return backlog, result


def test_tick_rebuilds_stored_backlog_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog, result = run_tick(tmp_path, monkeypatch)
    document = json.loads(backlog.read_text())
    assert [item["pr"] for item in document["items"]] == ["omnimarket#4"]
    assert document["standing_tickets"] == {"process-fix": "OMN-22147"}
    status = next(row[2] for row in result.found if row[1] == "lab-fill-stored")
    assert "stored=1" in status
    drops = json.loads((backlog.parent / fill.DROPPED_FILE).read_text())
    assert {key: value["why"].split()[0] for key, value in drops.items()} == {
        "omnimarket#1": "pr-merged",
        "omnimarket#2": "pr-closed",
        "omnimarket#3": "pr-not-open",
    }


def refresh_request(
    tmp_path: Path,
    *,
    state: dict | None = None,
    issues: list | None = None,
    claimed: set[str] | None = None,
    owned: set[str] | None = None,
    readbacks: dict[str, str] | None = None,
):
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelPillarBacklogRefreshRequest,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    snapshot = tmp_path / "tickets.json"
    snapshot.write_text(
        json.dumps(
            {"captured_at": NOW, "operator": "fixture-operator", "issues": issues or []}
        )
    )
    return ModelPillarBacklogRefreshRequest(
        now=NOW,
        state_dir=tmp_path / "state",
        watcher_state=state if state is not None else watcher(),
        sources=ModelBacklogSources(
            linear_snapshot=snapshot,
            ledger_tickets=lambda: (claimed or set(), readbacks or {}),
            pr_claims=lambda: owned or set(),
        ),
    )


def refresh(request):
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_pillar_backlog_refresh import (
        HandlerPillarBacklogRefresh,
    )

    return HandlerPillarBacklogRefresh().handle(request)


def test_live_prs_defects_readbacks_and_ownership(tmp_path: Path) -> None:
    state = watcher()
    for n, title in (
        (6, "Repair omn-22147"),
        (7, "Registry-owned repair"),
        (8, "Stalled checks"),
        (9, "Stale CI head"),
    ):
        state["prs"][f"omnimarket#{n}"] = json.loads(
            json.dumps(state["prs"]["omnimarket#4"])
        )
        state["prs"][f"omnimarket#{n}"]["facts"]["title"] = title
    state["prs"]["omnimarket#8"].update(
        {"cls": "pending", "ci": {"verdict": "PENDING"}}
    )
    state["prs"]["omnimarket#8"]["facts"]["updated_at"] = "2026-11-02T10:00:00Z"
    state["prs"]["omnimarket#9"]["ci"]["sha"] = "b" * 40
    issues = [
        {
            "identifier": "OMN-11",
            "title": "Open defect",
            "labels": [{"name": "bug"}],
            "state": {"type": "unstarted"},
            "repo": "omnimarket",
        },
        {
            "identifier": "OMN-12",
            "title": "Closed defect",
            "labels": ["defect"],
            "state": {"type": "completed"},
        },
        {
            "identifier": "OMN-13",
            "title": "Read back deployment",
            "labels": [],
            "status": "Backlog",
            "repo": "omnimarket",
        },
        {
            "identifier": "OMN-14",
            "title": "Other owner's defect",
            "labels": ["bug"],
            "assignee": "someone-else",
        },
        {"identifier": "OMN-15", "title": "Claimed defect", "labels": ["bug"]},
    ]
    request = refresh_request(
        tmp_path,
        state=state,
        issues=issues,
        claimed={"OMN-22147", "OMN-15"},
        owned={"OmniNode-ai/omnimarket#7"},
        readbacks={"OMN-13": "deployment needs readback"},
    )
    result = refresh(request)
    assert result.error == ""
    assert {item["key"] for item in result.items} == {
        "omnimarket#4",
        "omnimarket#8",
        "defect:OMN-11",
        "readback:OMN-13",
    }
    assert {item["fact_at"] for item in result.items} == {NOW}
    assert result.written
    assert not (request.state_dir / fill.SENT_FILE).exists()


def test_read_only_tick_projects_without_any_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog, result = run_tick(tmp_path, monkeypatch, read_only=True)
    assert len(json.loads(backlog.read_text())["items"]) == 4
    assert not (backlog.parent / fill.DROPPED_FILE).exists()
    status = next(row[2] for row in result.found if row[1] == "lab-fill-stored")
    assert "stored=1" in status
    assert "written=false" in status


@pytest.mark.parametrize(
    "broken",
    [
        "stale-watcher",
        "stale-resync",
        "unread-tickets",
        "stale-tickets",
        "unread-claims",
        "unread-pr-claims",
        "malformed-drop-record",
    ],
)
def test_unread_sources_preserve_file_and_offer_no_stored_work(
    tmp_path: Path, broken: str
) -> None:
    from dataclasses import replace

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    original = '{"items":[{"kind":"process-fix","key":"old"}]}\n'
    path.write_text(original)
    if broken == "stale-watcher":
        request.watcher_state["last_tick"] = "2026-11-01T16:00:00Z"
    elif broken == "stale-resync":
        request.watcher_state["last_full_resync"] = "2026-11-01T16:00:00Z"
    elif broken == "unread-tickets":
        request.sources.linear_snapshot.unlink()
    elif broken == "stale-tickets":
        request.sources.linear_snapshot.write_text(
            json.dumps({"captured_at": "2026-10-29T16:00:00Z", "issues": []})
        )
    elif broken == "malformed-drop-record":
        (request.state_dir / fill.DROPPED_FILE).write_text("[]")
    else:

        def unread():
            raise OSError("fixture source unavailable")

        field = "ledger_tickets" if broken == "unread-claims" else "pr_claims"
        request = replace(request, sources=replace(request.sources, **{field: unread}))
    result = refresh(request)
    assert result.error
    assert result.items == []
    assert not result.written
    assert path.read_text() == original


def test_unread_sources_keep_standing_tickets_and_unmanaged_kinds(
    tmp_path: Path,
) -> None:
    request = refresh_request(tmp_path)
    request.watcher_state["last_tick"] = "2026-11-01T16:00:00Z"
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    path.write_text(
        json.dumps(
            {
                "standing_tickets": {"dev-red": "OMN-1"},
                "items": [
                    {"kind": "process-fix", "key": "managed"},
                    {"kind": "m4-board-red", "key": "unmanaged"},
                ],
            }
        )
    )
    result = refresh(request)
    assert result.error
    assert result.standing_tickets == {"dev-red": "OMN-1"}
    assert [row["key"] for row in result.items] == ["unmanaged"]
    assert not result.written


def test_a_file_with_only_standing_tickets_is_an_empty_item_list(
    tmp_path: Path,
) -> None:
    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    path.write_text(json.dumps({"standing_tickets": {"dev-red": "OMN-1"}}))
    result = refresh(request)
    assert result.error == ""
    assert result.standing_tickets == {"dev-red": "OMN-1"}
    assert {row["key"] for row in result.items} == {"omnimarket#4"}


def test_write_is_atomic_and_holds_the_existing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    target = request.state_dir / fill.BACKLOG_FILE
    target.write_text('{"items": []}')
    original = Path.replace
    observed = []

    def checked_replace(source: Path, destination: Path) -> Path:
        if destination == target:
            assert json.loads(target.read_text()) == {"items": []}
            assert json.loads(source.read_text())["items"][0]["key"] == "omnimarket#4"
            with (
                (request.state_dir / fill.LOCK_FILE).open("a") as contender,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(destination)
        return original(source, destination)

    monkeypatch.setattr(Path, "replace", checked_replace)
    assert refresh(request).written
    assert observed == [target]
    assert not list(request.state_dir.glob("*.tmp"))


def test_tick_refresh_and_dispatch_share_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    observations = []

    def send(argv: list[str]) -> tuple[bool, str]:
        state = tmp_path / "state"
        assert [
            item["pr"]
            for item in json.loads((state / fill.BACKLOG_FILE).read_text())["items"]
        ] == ["omnimarket#4"]
        with (
            (state / fill.LOCK_FILE).open("a") as contender,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observations.append(argv)
        return True, "DETACHED"

    monkeypatch.setattr(fill, "_send", send)
    backlog, _ = run_tick(
        tmp_path,
        monkeypatch,
        hosts=[{"name": "host-a", "admission": {"claude_slots": 1}}],
    )
    assert len(observations) == 1
    assert list(json.loads((backlog.parent / fill.SENT_FILE).read_text())) == [
        "omnimarket#4"
    ]


def test_two_profile_declared_fixture_has_identical_stored_projection(
    tmp_path: Path,
) -> None:
    """Use the repository's local/lab binding fixture; this is not a live broker test."""
    from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
        compare_runs,
    )
    from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
        ModelScenarioRow,
        ModelScenarioRun,
    )
    from tests.nodes.node_idle_slot_fill_effect.test_fill_binding_parity import overlay

    declared = overlay("reference")
    runs = []
    for profile in declared.profiles:
        assert declared.binding_id(profile.id, "BKL") == "reference"
        request = refresh_request(
            tmp_path / profile.id, state=json.loads(json.dumps(watcher()))
        )
        result = refresh(request)
        assert result.error == ""
        assert [item["key"] for item in result.items] == ["omnimarket#4"]
        runs.append(
            ModelScenarioRun(
                profile_id=profile.id,
                binding=profile.id,
                proof_eligible=True,
                verdicts=("PASS",),
                rows=tuple(
                    ModelScenarioRow(
                        key=item["key"], content=json.dumps(item, sort_keys=True)
                    )
                    for item in result.items
                ),
                counts={"stored": len(result.items), "dropped": len(result.dropped)},
            )
        )
    compared = compare_runs(tuple(runs))
    assert compared.passed, compared.first_difference
    altered = runs[1].model_copy(update={"rows": ()})
    assert not compare_runs((runs[0], altered)).passed


def test_deployment_overlay_controls_paths_sources_and_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    import yaml

    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_pillar_backlog_refresh import (
        load_policy,
    )
    from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
        IDLE_SLOT_FILL,
        write_overlay,
    )

    config = load_policy().model_dump(mode="json")
    config.update(
        sources=["defect"],
        defect_labels=["incident"],
        backlog_file="live-work.json",
        dropped_file="live-drops.json",
    )
    env = write_overlay(
        tmp_path / "overlay",
        idle_slot_fill={**IDLE_SLOT_FILL, "backlog_refresh": config},
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", env["ONEX_SKILL_OVERLAY_ROOTS"])
    request = refresh_request(
        tmp_path / "run",
        issues=[
            {
                "id": "OMN-11",
                "title": "Declared defect source",
                "labels": ["incident"],
                "status": "Backlog",
            }
        ],
    )
    contract = yaml.safe_load(
        (Path(fill.__file__).parents[1] / "contract.yaml").read_text()
    )
    route = next(
        row
        for row in contract["handler_routing"]["handlers"]
        if row["operation"] == "refresh-backlog"
    )
    handler = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )()
    result = handler.handle(request)
    assert result.error == ""
    assert [item["key"] for item in result.items] == ["defect:OMN-11"]
    assert (request.state_dir / "live-work.json").is_file()
    assert (request.state_dir / "live-drops.json").is_file()
    assert not (request.state_dir / fill.BACKLOG_FILE).exists()
    assert not (request.state_dir / fill.DROPPED_FILE).exists()


def test_refresh_keeps_dispatch_identity_and_never_rewrites_sent_history(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelIdleSlotFillRequest,
    )

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    (request.state_dir / fill.BACKLOG_FILE).write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "process-fix",
                        "key": "previously-sent-key",
                        "pr": "omnimarket#4",
                        "repo": "omnimarket",
                    }
                ]
            }
        )
    )
    sent = {
        "previously-sent-key": {"at": "2026-11-02T15:00:00Z", "lane": "fixture-active"}
    }
    history = request.state_dir / fill.SENT_FILE
    history.write_text(json.dumps(sent))
    before = history.read_bytes()
    result = refresh(request)
    assert result.error == ""
    assert [item["key"] for item in result.items] == ["previously-sent-key"]
    assert history.read_bytes() == before
    chosen = fill.HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now=NOW,
            hosts=[{"name": "host-a", "admission": {"claude_slots": 1}}],
            lines=["MISSING lab-headroom:host-a | free Claude slots 1"],
            backlog=result.items,
            dispatched=sent,
            standing_tickets={"process-fix": "OMN-22147"},
            watcher_state=watcher(),
        )
    )
    assert chosen.sends == []
    assert any(
        row["key"] == "previously-sent-key"
        and row["why"].startswith("sent as fixture-active")
        for row in chosen.skipped
    )


def test_freshness_is_injected_and_does_not_re_date_unmanaged_work(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_pillar_backlog_refresh import (
        HandlerPillarBacklogRefresh,
        load_policy,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelPillarBacklogPolicy,
    )

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    previous = {
        "kind": "m4-board-red",
        "key": "old-board",
        "fact_at": "2026-11-02T14:00:00Z",
    }
    path.write_text(json.dumps({"items": [previous]}))
    policy = ModelPillarBacklogPolicy.model_validate(
        {**load_policy().model_dump(), "fact_max_age_seconds": 3600}
    )
    result = HandlerPillarBacklogRefresh(policy).handle(request)
    assert result.error == ""
    assert {row["key"] for row in result.items} == {"omnimarket#4"}
    assert result.dropped == [
        {
            "key": "old-board",
            "why": "fact-stale 2026-11-02T14:00:00Z (older than 48h)",
        }
    ]
    path.write_text(json.dumps({"items": [previous]}))
    result = HandlerPillarBacklogRefresh(
        ModelPillarBacklogPolicy.model_validate(
            {**policy.model_dump(), "fact_max_age_seconds": 7200}
        )
    ).handle(request)
    assert next(row for row in result.items if row["key"] == "old-board") == previous


@pytest.mark.parametrize(
    "broken", ["record", "ci", "coverage", "backlog", "tickets", "registry"]
)
def test_malformed_inputs_never_replace_the_stored_file(
    tmp_path: Path, broken: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "process-fix",
                        "key": "missing-repo#6"
                        if broken == "coverage"
                        else "omnimarket#4",
                    }
                ]
            }
        )
    )
    if broken == "record":
        request.watcher_state["prs"]["omnimarket#4"] = None
    elif broken == "ci":
        request.watcher_state["prs"]["omnimarket#4"]["ci"] = "unread"
    elif broken == "backlog":
        path.write_text('{"items": "unread"}')
    elif broken == "tickets":
        request.sources.linear_snapshot.write_text('{"issues": "unread"}')
    elif broken == "registry":
        directory = tmp_path / "claims"
        directory.mkdir()
        (directory / "broken.json").write_text("[]")
        monkeypatch.setenv("ONEX_PR_CLAIMS_DIR", str(directory))
        request = replace(request, sources=replace(request.sources, pr_claims=None))
    original = path.read_bytes()
    result = refresh(request)
    assert result.error
    assert result.items == []
    assert path.read_bytes() == original


def test_generation_off_does_not_disable_refresh_or_reenable_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("generation must remain disabled")

    monkeypatch.setattr(fill, "read_generated", forbidden)
    monkeypatch.setattr(fill, "_send", lambda _argv: (True, "DETACHED"))
    backlog, result = run_tick(
        tmp_path,
        monkeypatch,
        default_binding=True,
        hosts=[{"name": "host-a", "admission": {"claude_slots": 2}}],
    )
    assert [item["pr"] for item in json.loads(backlog.read_text())["items"]] == [
        "omnimarket#4"
    ]
    assert any(
        row[1] == "lab-fill-stored" and "stored=1" in row[2] for row in result.found
    )


def test_invalid_output_policy_never_overwrites_dispatch_history(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_pillar_backlog_refresh import (
        HandlerPillarBacklogRefresh,
        load_policy,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelPillarBacklogPolicy,
    )

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.SENT_FILE
    original = '{"already-sent": {"at": "2026-11-02T15:00:00Z"}}'
    path.write_text(original)
    policy = ModelPillarBacklogPolicy.model_validate(
        {**load_policy().model_dump(), "backlog_file": fill.SENT_FILE}
    )
    result = HandlerPillarBacklogRefresh(policy).handle(request)
    assert result.error.startswith("ValueError: refresh paths collide")
    assert path.read_text() == original
    assert not result.written


def test_deployment_reads_all_live_ticket_pages_each_tick(tmp_path: Path) -> None:
    from dataclasses import replace

    request = refresh_request(
        tmp_path, readbacks={"OMN-13": "deployment needs readback"}
    )
    calls = []

    def post(query, variables):
        calls.append(json.loads(json.dumps(variables)))
        ticket = "OMN-11" if len(calls) == 1 else "OMN-13"
        issue = {
            "identifier": ticket,
            "title": "Live open work",
            "state": {"type": "unstarted"},
            "labels": {"nodes": [{"name": "bug"}]},
            "assignee": {"id": "fixture-operator"},
        }
        return {
            "viewer": {"id": "fixture-operator"},
            "issues": {
                "nodes": [issue],
                "pageInfo": {"hasNextPage": len(calls) == 1, "endCursor": "next"},
            },
        }

    request = replace(
        request,
        sources=replace(request.sources, linear_snapshot=None, ticket_post=post),
    )
    result = refresh(request)
    assert result.error == ""
    assert {row["key"] for row in result.items} == {
        "omnimarket#4",
        "defect:OMN-11",
        "readback:OMN-13",
    }
    assert len(calls) == 2
    assert calls[0]["after"] is None
    assert calls[1]["after"] == "next"
    assert calls[0]["filter"]["state"] == {"type": {"nin": ["completed", "canceled"]}}
    assert calls[0]["filter"]["or"] == [
        {"labels": {"some": {"name": {"in": ["bug", "defect"]}}}},
        {"number": {"in": [13]}},
    ]
    calls.clear()
    assert refresh(request).written
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "api-unread",
        "pagination-unread",
        "repeated-cursor",
        "page-limit",
        "malformed-ticket",
    ],
)
def test_unread_or_incomplete_live_ticket_read_never_replaces_backlog(
    tmp_path: Path, failure: str
) -> None:
    from dataclasses import replace

    from omnimarket.handlers.handler_lab_fill_live_tickets import LabFillLiveReadError
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_pillar_backlog_refresh import (
        HandlerPillarBacklogRefresh,
        load_policy,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelPillarBacklogPolicy,
    )

    request = refresh_request(tmp_path)
    request.state_dir.mkdir()
    path = request.state_dir / fill.BACKLOG_FILE
    original = '{"items": []}\n'
    path.write_text(original)
    calls = []

    def post(query, variables):
        calls.append(dict(variables))
        if failure == "api-unread":
            raise LabFillLiveReadError("fixture API unavailable")
        return {
            "viewer": {"id": "fixture-operator"},
            "issues": {
                "nodes": [None] if failure == "malformed-ticket" else [],
                "pageInfo": None
                if failure == "pagination-unread"
                else {"hasNextPage": True, "endCursor": "same"},
            },
        }

    request = replace(
        request,
        sources=replace(request.sources, linear_snapshot=None, ticket_post=post),
    )
    policy = ModelPillarBacklogPolicy.model_validate(
        {
            **load_policy().model_dump(),
            "ticket_max_pages": 1 if failure == "page-limit" else 2,
        }
    )
    result = HandlerPillarBacklogRefresh(policy).handle(request)
    assert result.error
    assert result.items == []
    assert path.read_text() == original
    assert calls


def test_default_source_binding_uses_live_ticket_reader_even_when_generator_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    assert fill.default_sources(NOW, {}) is not None
    assert fill.default_sources(NOW, {"OMNI_LAB_FILL_GENERATE": "off"}) is None
    assert (
        fill.live_sources(NOW, {"OMNI_LAB_FILL_GENERATE": "off"}).ticket_post
        is fill.linear_post
    )


def test_no_headroom_tick_records_the_stored_tier_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = []

    def record(action: str, subject: str, detail: str) -> None:
        recorded.append((action, subject, detail))

    _, result = run_tick(tmp_path, monkeypatch, recorder=record)
    assert len(recorded) == 1
    assert recorded[0][:2] == ("lab-fill-backlog-refresh", "stored-tier")
    assert "stored=1" in recorded[0][2]
    assert "written=true" in recorded[0][2]
    assert any(row[1] == "lab-fill-stored" for row in result.found)


def test_failed_status_record_is_named_after_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog, result = run_tick(
        tmp_path,
        monkeypatch,
        recorder=lambda _action, _subject, _detail: "fixture bus unavailable",
    )
    assert len(json.loads(backlog.read_text())["items"]) == 1
    assert any(
        row[1] == "lab-fill-stored-status" and "fixture bus unavailable" in row[2]
        for row in result.found
    )


def test_new_ticket_items_cannot_reintroduce_a_retired_pr_reference(
    tmp_path: Path,
) -> None:
    request = refresh_request(
        tmp_path,
        issues=[
            {
                "id": "OMN-11",
                "title": "Repair omnimarket#1",
                "labels": ["bug"],
                "status": "Backlog",
            }
        ],
    )
    result = refresh(request)
    assert result.error == ""
    assert [item["key"] for item in result.items] == ["omnimarket#4"]
    assert result.dropped == [{"key": "defect:OMN-11", "why": "pr-merged omnimarket#1"}]


def test_contract_refresh_request_crosses_a_json_boundary_with_di_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import TypeAdapter

    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        handler_pillar_backlog_refresh as refresh_module,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models import (
        ModelPillarBacklogRefreshRequest,
    )

    injected = refresh_request(tmp_path)
    monkeypatch.setattr(
        refresh_module, "live_sources", lambda _now, _env: injected.sources
    )
    request = TypeAdapter(ModelPillarBacklogRefreshRequest).validate_json(
        json.dumps(
            {
                "now": NOW,
                "state_dir": str(injected.state_dir),
                "watcher_state": watcher(),
            }
        )
    )
    assert request.state_dir == injected.state_dir
    assert request.sources is None
    result = refresh_module.HandlerPillarBacklogRefresh().handle(request)
    assert result.error == ""
    assert [row["key"] for row in result.items] == ["omnimarket#4"]
