# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sweep dispatcher refuses missing repository identity before any runner call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_merge_sweep_dispatch_orchestrator.handlers.handler_merge_sweep_dispatch import (
    HandlerMergeSweepDispatch,
)
from omnimarket.nodes.node_merge_sweep_dispatch_orchestrator.models.model_merge_sweep_dispatch import (
    ModelMergeSweepDispatchLane,
    ModelMergeSweepDispatchRequest,
)


def test_planned_lane_without_repo_is_refused_before_dispatch(tmp_path: Path) -> None:
    calls: list[object] = []

    def dispatch(*args: object) -> object:
        calls.append(args)
        raise AssertionError("a refused plan must never reach the dispatch runner")

    lane = ModelMergeSweepDispatchLane(
        lane="sweep-diagnose-1",
        kind="diagnose",
        repo=None,
        ticket="OMN-19998",
        record_json=json.dumps({"repo": "example/widgets"}),
    )
    result = HandlerMergeSweepDispatch(dispatch=dispatch).handle(
        ModelMergeSweepDispatchRequest(
            parent="sweep", lanes=(lane,), output_dir=str(tmp_path), execute=True
        )
    )
    assert result.outcomes[0].status == "refused"
    assert result.outcomes[0].error_code == "REMOTE-LANE-JOB-SPEC-REFUSED"
    assert "repo" in result.outcomes[0].detail
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def prepared_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, kind: str = "fix"
) -> ModelMergeSweepDispatchLane:
    from omnimarket.models.lane_dispatch.model_lane_dispatch import (
        ModelLaneDispatchRecord,
    )
    from tests.nodes.test_lane_dispatch_contract import configure_runtime

    request = configure_runtime(tmp_path, monkeypatch)
    raw = json.loads(request.record_json)
    raw["spec"]["lane_kind"] = {
        "fix": "conflict-fix",
        "close": "ticket",
        "land-chain-head": "ticket",
        "diagnose": "review-only",
    }[kind]
    raw["spec"]["task"] = (
        "Update this repository. Host pin: controller files are on this host."
    )
    # This is the sender preparing a record before submission, never the dispatcher repairing one.
    from omnimarket.models.lane_dispatch.model_lane_dispatch import (
        ModelLaneDispatchSpec,
    )

    record = ModelLaneDispatchRecord.from_spec(
        ModelLaneDispatchSpec.model_validate(raw["spec"])
    )
    return ModelMergeSweepDispatchLane(
        lane="dispatch-proof",
        kind=kind,
        repo="widgets",
        ticket="OMN-19998",
        prs=("widgets#42",),
        host="h053",
        host_why="controller files are on this host",
        record_json=record.canonical_json(),
    )


def sweep_request(
    tmp_path: Path, *lanes: ModelMergeSweepDispatchLane, **kwargs: object
) -> ModelMergeSweepDispatchRequest:
    return ModelMergeSweepDispatchRequest(
        parent="orchestrator-31d9",
        lanes=lanes,
        output_dir=str(tmp_path / "sweep-artifacts"),
        execute=True,
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["fix", "close", "land-chain-head", "diagnose"])
def test_valid_planned_lane_uses_the_contract_bus_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from omnimarket.models.lane_dispatch.model_lane_dispatch import (
        ModelLaneDispatchRecord,
    )

    lane = prepared_lane(tmp_path, monkeypatch, kind=kind)
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert result.ok, result
    [outcome] = result.outcomes
    assert outcome.status == "submitted"
    argv = json.loads((tmp_path / "runner-observed.json").read_text())
    for flag, value in (
        ("--lane", lane.lane),
        ("--ticket", lane.ticket),
        ("--repo", lane.repo),
        ("--parent", "orchestrator-31d9"),
        ("--host", lane.host),
        ("--mem-max", "8589934592"),
    ):
        assert argv[argv.index(flag) + 1] == value
    record = ModelLaneDispatchRecord.model_validate_json(lane.record_json)
    assert outcome.content_hash == record.content_hash
    assert Path(outcome.record_path).read_text().strip() == lane.record_json
    assert lane.host_why in Path(outcome.brief_path).read_text()
    assert result.deferred == 0


@pytest.mark.parametrize("repo", [None, "", "  "])
def test_missing_plan_repo_never_reaches_the_real_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str | None
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch).model_copy(update={"repo": repo})
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert not result.ok
    assert result.outcomes[0].error_code == "REMOTE-LANE-JOB-SPEC-REFUSED"
    assert not (tmp_path / "runner-observed.json").exists()
    assert not (tmp_path / "sweep-artifacts").exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"repo": "other"},
        {"repo": "other/widgets"},
        {"ticket": "OMN-1"},
        {"lane": "wrong-lane"},
        {"prs": ()},
        {"host": "h051"},
        {"host_why": "a reason the record does not carry"},
    ],
)
def test_plan_and_record_identity_disagreement_is_refused_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: dict[str, object]
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch).model_copy(update=patch)
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert result.outcomes[0].error_code == "DISPATCH-IDENTITY-REFUSED"
    assert not (tmp_path / "runner-observed.json").exists()


def test_plan_parent_disagreement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch)
    request = sweep_request(tmp_path, lane).model_copy(update={"parent": "other"})
    result = HandlerMergeSweepDispatch().handle(request)
    assert result.outcomes[0].error_code == "DISPATCH-IDENTITY-REFUSED"
    assert not (tmp_path / "runner-observed.json").exists()


@pytest.mark.parametrize("kind", ["escalation", "unknown"])
def test_kind_the_frozen_dispatcher_refused_stays_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch).model_copy(update={"kind": kind})
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert result.outcomes[0].error_code == "SWEEP-KIND-REFUSED"
    assert not (tmp_path / "runner-observed.json").exists()


def test_diagnose_repo_and_controller_pin_reach_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch, kind="diagnose")
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert result.ok, result
    argv = json.loads((tmp_path / "runner-observed.json").read_text())
    assert argv[argv.index("--repo") + 1] == "widgets"
    assert argv[argv.index("--host") + 1] == "h053"


def test_tampered_envelope_is_not_rehashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch)
    record = json.loads(lane.record_json)
    record["spec"]["task"] += " Tampered."
    lane = lane.model_copy(update={"record_json": json.dumps(record)})
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert result.outcomes[0].error_code == "DISPATCH-HASH-MISMATCH"
    assert not (tmp_path / "runner-observed.json").exists()


def test_cap_and_duplicate_names_do_not_start_duplicate_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch)
    result = HandlerMergeSweepDispatch().handle(
        sweep_request(tmp_path, lane, max_lanes=0)
    )
    assert result.ok
    assert result.outcomes == ()
    assert result.deferred == 1
    duplicate = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane, lane))
    assert not duplicate.ok
    assert len(duplicate.outcomes) == 2
    assert all(
        outcome.error_code == "DISPATCH-IDENTITY-REFUSED"
        for outcome in duplicate.outcomes
    )
    assert not (tmp_path / "runner-observed.json").exists()


def test_sweep_dispatch_node_is_requested_and_answered_over_the_runtime_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
    from omnibase_core.runtime.runtime_local import RuntimeLocal

    lane = prepared_lane(tmp_path, monkeypatch)
    request = sweep_request(tmp_path, lane)
    input_path = tmp_path / "sweep-request.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=Path(
            "src/omnimarket/nodes/node_merge_sweep_dispatch_orchestrator/contract.yaml"
        ),
        input_path=input_path,
        state_root=tmp_path / "runtime",
        timeout=60,
        backend_overrides={"event_bus": "inmemory"},
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED, runtime.last_error
    result = runtime.handler_result
    assert result.ok, result
    assert result.outcomes[0].status == "submitted"
    assert (tmp_path / "runner-observed.json").exists()


def test_checker_refusal_propagates_without_runner_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch)
    (tmp_path / "checker.py").write_text("raise SystemExit(2)\n")
    result = HandlerMergeSweepDispatch().handle(sweep_request(tmp_path, lane))
    assert not result.ok
    assert result.outcomes[0].error_code == "LANE-BRIEF-REFUSED"
    assert not (tmp_path / "runner-observed.json").exists()


def test_check_only_request_never_executes_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = prepared_lane(tmp_path, monkeypatch)
    request = sweep_request(tmp_path, lane).model_copy(update={"execute": False})
    result = HandlerMergeSweepDispatch().handle(request)
    assert result.ok, result
    assert result.outcomes[0].status == "checked"
    assert result.outcomes[0].runner_argv
    assert not (tmp_path / "runner-observed.json").exists()


def test_each_admitted_plan_record_is_forwarded_once_and_cap_keeps_order(
    tmp_path: Path,
) -> None:
    from omnimarket.models.lane_dispatch.model_lane_dispatch_effect import (
        ModelLaneDispatchRequest,
        ModelLaneDispatchResult,
    )

    calls: list[ModelLaneDispatchRequest] = []

    def dispatch(request: ModelLaneDispatchRequest) -> ModelLaneDispatchResult:
        calls.append(request)
        return ModelLaneDispatchResult(status="checked")

    lanes = tuple(
        ModelMergeSweepDispatchLane(
            lane=f"sweep-fix-{i}",
            kind="fix",
            repo="widgets",
            ticket="OMN-19998",
            record_json=json.dumps(
                {
                    "lane": f"sweep-fix-{i}",
                    "repo": "example/widgets",
                    "ticket": "OMN-19998",
                    "parent": "orchestrator-31d9",
                    "lane_kind": "conflict-fix",
                    "pr": [],
                }
            ),
        )
        for i in range(1, 4)
    )
    result = HandlerMergeSweepDispatch(dispatch=dispatch).handle(
        sweep_request(tmp_path, *lanes, max_lanes=2)
    )
    assert result.ok
    assert result.deferred == 1
    assert [call.record_json for call in calls] == [
        lane.record_json for lane in lanes[:2]
    ]
    assert [outcome.lane for outcome in result.outcomes] == [
        lane.lane for lane in lanes[:2]
    ]
