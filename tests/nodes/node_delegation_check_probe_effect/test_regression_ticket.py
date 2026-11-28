# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation check that reads red after green opens one top-priority ticket and never a second while it is open (OMN-26726).

Exemplar: test_tick_single_check.py (the tick's evidence) and the regression node's one-alarm-per-turn-red rule.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_regression_ticket import (
    HandlerRegressionTicket,
    JsonRegressionStateStore,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_regression_ticket import (
    EnumRegressionAction,
    ModelRegressionRunReading,
    ModelRegressionTicketRequest,
)


class FakeWriter:
    def __init__(self) -> None:
        self.requests: list[ModelCreateTicketRequest] = []
        self.status = "In Progress"
        self.fail = False

    def send(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("linear down")
        if request.operation is EnumTicketOperation.CREATE:
            return ModelCreateTicketResult(
                status="created", ticket_id=f"OMN-9{len(self.requests):03d}"
            )
        if request.operation is EnumTicketOperation.READ:
            return ModelCreateTicketResult(
                status="read", operation=request.operation, ticket_status=self.status
            )
        return ModelCreateTicketResult(
            status="commented", operation=request.operation, comment_id="c1"
        )

    def ops(self, op: EnumTicketOperation) -> list[ModelCreateTicketRequest]:
        return [r for r in self.requests if r.operation is op]


def _run(
    handler: HandlerRegressionTicket,
    run_id: str,
    verdict: EnumCheckVerdict,
    step: str = "",
    check: str = "C14",
):
    return handler.handle(
        ModelRegressionTicketRequest(
            reading=ModelRegressionRunReading(
                check_id=check, run_id=run_id, verdict=verdict, failing_step=step
            )
        )
    )


def _handler(tmp_path, writer):
    return HandlerRegressionTicket(
        store=JsonRegressionStateStore(tmp_path / "state.json"),
        writer=writer,
        parent="OMN-26718",
        stamp="actor: probe",
    )


def test_green_then_red_opens_one_ticket_naming_check_run_step_and_last_green(
    tmp_path,
):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "run-1", EnumCheckVerdict.PASS)
    res = _run(h, "run-2", EnumCheckVerdict.FAIL, "routing_tier_order")
    creates = w.ops(EnumTicketOperation.CREATE)
    assert len(creates) == 1
    text = creates[0].title + creates[0].description
    for fact in ("C14", "run-2", "routing_tier_order", "run-1"):
        assert fact in text
    assert "Priority: Urgent" in creates[0].description
    assert creates[0].parent == "OMN-26718"
    assert res.action is EnumRegressionAction.OPENED
    assert res.ticket_id == "OMN-9001"


def test_green_red_red_opens_one_ticket_and_comments_the_second_run(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "run-1", EnumCheckVerdict.PASS)
    _run(h, "run-2", EnumCheckVerdict.FAIL, "s")
    res = _run(h, "run-3", EnumCheckVerdict.ERROR, "s2")
    assert len(w.ops(EnumTicketOperation.CREATE)) == 1
    comments = w.ops(EnumTicketOperation.COMMENT)
    assert len(comments) == 1
    assert comments[0].ticket_id == "OMN-9001"
    assert "run-3" in comments[0].body
    assert res.action is EnumRegressionAction.COMMENTED


def test_red_then_green_comments_the_recovery_and_closes_nothing(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "run-1", EnumCheckVerdict.PASS)
    _run(h, "run-2", EnumCheckVerdict.FAIL, "s")
    res = _run(h, "run-3", EnumCheckVerdict.PASS)
    assert not w.ops(EnumTicketOperation.TRANSITION)
    rec = w.ops(EnumTicketOperation.COMMENT)
    assert len(rec) == 1
    assert "run-3" in rec[0].body
    assert res.action is EnumRegressionAction.RECOVERY_COMMENTED
    # a second green does not comment again
    _run(h, "run-4", EnumCheckVerdict.PASS)
    assert len(w.ops(EnumTicketOperation.COMMENT)) == 1


def test_red_after_recovery_comments_the_still_open_ticket_and_opens_no_second(
    tmp_path,
):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    for rid, v in [
        ("1", EnumCheckVerdict.PASS),
        ("2", EnumCheckVerdict.FAIL),
        ("3", EnumCheckVerdict.PASS),
    ]:
        _run(h, rid, v, "s")
    _run(h, "4", EnumCheckVerdict.FAIL, "s")
    assert len(w.ops(EnumTicketOperation.CREATE)) == 1


def test_red_after_the_ticket_was_closed_opens_a_new_one(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "1", EnumCheckVerdict.PASS)
    _run(h, "2", EnumCheckVerdict.FAIL, "s")
    w.status = "Done"
    _run(h, "3", EnumCheckVerdict.FAIL, "s")
    assert len(w.ops(EnumTicketOperation.CREATE)) == 2


def test_green_with_no_open_ticket_and_unmeasured_runs_write_nothing(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "1", EnumCheckVerdict.PASS)
    _run(h, "2", EnumCheckVerdict.NOT_MEASURED)
    _run(h, "3", EnumCheckVerdict.PRECONDITION_ABSENT)
    assert w.requests == []


def test_a_failed_create_is_retried_by_the_next_red_run(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "1", EnumCheckVerdict.PASS)
    w.fail = True
    res = _run(h, "2", EnumCheckVerdict.FAIL, "s")
    assert res.action is EnumRegressionAction.FAILED
    w.fail = False
    _run(h, "3", EnumCheckVerdict.FAIL, "s")
    creates = [r for r in w.requests if r.operation is EnumTicketOperation.CREATE]
    assert len(creates) == 2
    assert "Last green run: 1" in creates[1].description


def test_checks_keep_separate_state(tmp_path):
    w = FakeWriter()
    h = _handler(tmp_path, w)
    _run(h, "a1", EnumCheckVerdict.PASS, check="C13")
    _run(h, "b1", EnumCheckVerdict.FAIL, "s", check="C14")
    _run(h, "a2", EnumCheckVerdict.FAIL, "s", check="C13")
    assert len(w.ops(EnumTicketOperation.CREATE)) == 2


def test_the_scheduled_tick_files_one_ticket_for_a_red_check_then_comments_and_a_manual_run_files_none(
    tmp_path, monkeypatch
):
    import json

    import yaml
    from _gateway_fake import FakeGateway
    from test_tick_single_check import _overlays

    from omnimarket.nodes.node_delegation_check_probe_effect import (
        runtime_delegation_check_probe_tick as tick,
    )
    from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
        InMemoryCheckPublisher,
    )

    env = _overlays(tmp_path, ["C17"])
    probe = yaml.safe_load(Path(env[tick.OVERLAY_FILE_ENV]).read_text())
    probe["regression_ticket"] = {
        "parent": "OMN-26718",
        "lane": "probe",
        "state_file": "tickets.json",
    }
    Path(env[tick.OVERLAY_FILE_ENV]).write_text(yaml.safe_dump(probe))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    w = FakeWriter()
    seq = iter(["a", "b", "c"])

    def red_run(self, request):
        from datetime import UTC, datetime

        from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
            ModelCheckClause,
            ModelDelegationCheckResult,
        )

        now = datetime.now(UTC)
        return ModelDelegationCheckResult(
            correlation_id=request.correlation_id,
            check_id=request.check_id,
            run_id=f"run-{next(seq)}",
            created_at=now,
            finished_at=now,
            event=request.trigger,
            status="completed",
            conclusion="failure",
            verdict=EnumCheckVerdict.FAIL,
            clauses=(ModelCheckClause(name="surface_guide", ok=False, detail="x"),),
            emitted=True,
        )

    monkeypatch.setattr(tick.HandlerDelegationCheckProbe, "handle", red_run)

    def go(*extra):
        tick.main(
            [
                "--omni-home",
                str(tmp_path),
                "--state-dir",
                str(tmp_path / "state"),
                *extra,
            ],
            publisher=InMemoryCheckPublisher(),
            gateway=FakeGateway(),
            ticket_writer=w,
        )
        return json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())

    go("--trigger", "manual")
    assert w.requests == []
    ev = go()
    run = ev["runs"][0]
    assert run["verdict"] == "FAIL", run
    assert "surface_guide" in w.ops(EnumTicketOperation.CREATE)[0].title
    assert ev["regression_tickets"][0]["action"] == "opened"
    go()
    assert len(w.ops(EnumTicketOperation.CREATE)) == 1
    assert len(w.ops(EnumTicketOperation.COMMENT)) == 1
