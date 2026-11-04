# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Carry-over safety and capture health through the ordinary effect boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.model_capture_health import (
    ModelCaptureHealthRequest,
    ModelClassifierObservation,
)
from omnimarket.models.model_carry_over import (
    ModelCarryOverEvidence,
    ModelCarryOverLabel,
    ModelCarryOverRequest,
)
from omnimarket.models.operator_capture import ModelCaptureProcessRequest
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    HandlerAskStateReport,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_carry_over import (
    HandlerCarryOver,
)
from omnimarket.nodes.node_ask_state_effect.models.model_ask_state_read import (
    ModelAskStateReadRequest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
    load_capture_health_policy,
)
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerCaptureHealth,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 4, 19, tzinfo=UTC)
SINCE = NOW - timedelta(days=3)


def sources(tmp_path: Path, count: int = 3) -> ModelAskStateReadRequest:
    store = tmp_path / "captures"
    store.mkdir()
    records = [
        {
            "capture_id": f"capture-{i}",
            "session_id": "s",
            "received_at": SINCE.isoformat(),
            "text": f"Check item {i}.",
            "status": "recorded",
            "classification": {
                "origin": "operator",
                "classifier": "heuristic",
                "items": [
                    {"item_id": f"cap-{i}", "kind": "ask", "quote": f"Check item {i}."}
                ],
            },
        }
        for i in range(count)
    ]
    (store / "captures.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("")
    return ModelAskStateReadRequest(
        captures_dir=store, ledger_path=ledger, since=SINCE, now=NOW, archives=False
    )


def row(kind: str, ask: str, cells: str) -> str:
    return f"2026-11-04T18:00:00Z | {kind} | lane=orchestrator | ask={ask} | {cells}"


def test_carry_over_first_empty_classifier_call_keeps_heuristic_without_alarm(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    appended: list[str] = []

    def delegate(prompt: str, contract: object) -> DelegateAnswer:
        calls.append(prompt)
        return DelegateAnswer(None, None, "the delegate receipt carries no response")

    capture_store.ingest(
        tmp_path,
        session_id="s",
        text="Check the queue.",
        source="bus:test",
        received_at=NOW,
        origin_event="content-captured",
    )
    result = HandlerCaptureProcess(
        delegate, lambda text: (appended.append(text) is None, "ok"), now=NOW
    ).handle(
        ModelCaptureProcessRequest(
            store_dir=tmp_path, ledger_path=tmp_path / "ledger.md"
        )
    )
    record = json.loads((tmp_path / "captures.jsonl").read_text().splitlines()[0])
    assert len(calls) == 1
    assert record["classification"]["classifier"] == "heuristic"
    assert record["classification"]["call"]["outcome"] == "empty"
    assert result.findings == ()
    assert not any("kind=finding" in text for text in appended)


def test_carry_over_twenty_heuristic_prompts_raise_silent_failure_finding() -> None:
    observations = tuple(
        ModelClassifierObservation(capture_id=f"c{i}", classifier="heuristic")
        for i in range(20)
    )
    handler = HandlerCaptureHealth()
    policy = load_capture_health_policy()
    result = handler.handle(
        ModelCaptureHealthRequest(observations=observations, policy=policy)
    )
    assert len(result.findings) == 1
    assert result.findings[0].kind == "FINDING"
    assert result.findings[0].unanswered == 20
    assert (
        handler.handle(
            ModelCaptureHealthRequest(observations=observations[:19], policy=policy)
        ).findings
        == ()
    )
    answered = ModelClassifierObservation(capture_id="answered", classifier="delegated")
    assert (
        handler.handle(
            ModelCaptureHealthRequest(
                observations=(*observations, answered), policy=policy
            )
        ).findings
        == ()
    )


def test_carry_over_health_cause_is_measured_never_a_hedged_guess() -> None:
    policy = load_capture_health_policy()
    measured = ModelClassifierObservation(
        capture_id="c",
        classifier="heuristic",
        call={
            "outcome": "timeout",
            "command": "onex delegate --json",
            "observation": "TimeoutExpired after 330 seconds",
        },
    )
    result = HandlerCaptureHealth().handle(
        ModelCaptureHealthRequest(observations=(measured,), policy=policy)
    )
    assert result.causes == {"timeout": 1}
    assert result.measurements == (measured.call,)
    assert HandlerCaptureHealth().handle(
        ModelCaptureHealthRequest(
            observations=(
                ModelClassifierObservation(capture_id="old", classifier="heuristic"),
            ),
            policy=policy,
        )
    ).causes == {"unmeasured": 1}
    with pytest.raises(ValueError, match="outcome"):
        ModelClassifierObservation(
            capture_id="c",
            classifier="heuristic",
            call={
                "outcome": "probably timeout",
                "command": "probe",
                "observation": "maybe",
            },
        )


@pytest.mark.parametrize(
    "failure", ["not-called", "timeout", "unreachable", "empty", "invalid", "answered"]
)
def test_carry_over_production_adapter_measures_the_call_outcome(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
        onex_delegate_runner,
    )

    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 330)
        if failure == "unreachable":
            raise OSError("delegate runner unavailable")
        stdout = {
            "empty": '{"result": {"response": ""}}',
            "invalid": "not a JSON receipt",
            "answered": '{"result": {"response": "{\\"items\\":[]}", "model": "fixture-model"}}',
        }[failure]
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    answer = onex_delegate_runner(
        None if failure == "not-called" else "dev",
        {"ONEX_OPERATOR_CAPTURE_ONEX_BIN": "fixture-onex"},
    )("fixture prompt", {})
    assert answer.call is not None
    assert answer.call.outcome == failure
    assert bool(calls) == (failure != "not-called")
    assert answer.call.command
    assert answer.call.observation


def test_carry_over_ordinary_terminal_requires_its_cited_pr_to_be_merged(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path)
    read.ledger_path.write_text(
        row("TERMINAL", "ask-0", "outcome=done | pr=example#42") + "\n"
    )
    before = HandlerAskStateReport().handle(read)
    assert next(e for e in before.asks if e.ask_id == "ask-0").state.value != "done"
    after = HandlerAskStateReport().handle(
        read.model_copy(update={"merged_prs": ("example#42",)})
    )
    assert next(e for e in after.asks if e.ask_id == "ask-0").state.value == "done"


def test_carry_over_alarm_counts_prompts_once_and_policy_controls_the_bound() -> None:
    policy = load_capture_health_policy().model_copy(update={"unanswered_window": 2})
    one = ModelClassifierObservation(capture_id="one", classifier="heuristic")
    handler = HandlerCaptureHealth()
    assert (
        handler.handle(
            ModelCaptureHealthRequest(observations=(one,) * 20, policy=policy)
        ).findings
        == ()
    )
    second = ModelClassifierObservation(capture_id="two", classifier="heuristic")
    assert (
        handler.handle(
            ModelCaptureHealthRequest(observations=(one, second), policy=policy)
        )
        .findings[0]
        .unanswered
        == 2
    )


def test_carry_over_no_completion_without_same_ordinary_evidence_rule(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path)
    read.ledger_path.write_text(row("TERMINAL", "ask-0", "outcome=done") + "\n")
    result = HandlerCarryOver().handle(
        ModelCarryOverRequest(
            read=read,
            ask_ids=("ask-0", "ask-1", "ask-2"),
            evidence=(
                ModelCarryOverEvidence(ask_id="ask-1", reference="example#42"),
                ModelCarryOverEvidence(
                    ask_id="ask-2", reference="receipt:verified-change"
                ),
            ),
        )
    )
    assert {e.ask_id: e.state.value for e in result.report.asks} == {
        "ask-0": "owned",
        "ask-1": "owned",
        "ask-2": "done",
    }
    assert all(
        "state=done" not in r
        for r in result.proposed_rows
        if "ask=ask-0" in r or "ask=ask-1" in r
    )
    merged = HandlerCarryOver().handle(
        ModelCarryOverRequest(
            read=read.model_copy(update={"merged_prs": ("example#42",)}),
            ask_ids=("ask-1",),
            evidence=(ModelCarryOverEvidence(ask_id="ask-1", reference="example#42"),),
        )
    )
    assert (
        next(e for e in merged.report.asks if e.ask_id == "ask-1").state.value == "done"
    )


def test_carry_over_withdrawal_requires_correction_with_confirmed_label_run(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path)
    read.ledger_path.write_text(row("CORRECTION", "ask-0", "closes-ask=ask-0") + "\n")
    labels = (
        ModelCarryOverLabel(
            ask_id="ask-1",
            label="not-an-ask",
            run_id="label-run-1",
            engine="second-engine",
        ),
        ModelCarryOverLabel(
            ask_id="ask-2",
            label="not-an-ask",
            run_id="label-run-2",
            engine="second-engine",
            confirmed_by="orchestrator",
        ),
    )
    result = HandlerCarryOver().handle(
        ModelCarryOverRequest(
            read=read, ask_ids=("ask-0", "ask-1", "ask-2"), labels=labels
        )
    )
    assert result.counts.withdrawn == 1
    assert result.counts.owned == 2
    corrections = [r for r in result.proposed_rows if " | CORRECTION | " in r]
    assert len(corrections) == 1
    assert "label_run=label-run-2" in corrections[0]
    assert "confirmed_by=orchestrator" in corrections[0]
    assert result.decisions == ("ask-1",)


def test_carry_over_dry_run_218_items_is_read_only_and_replay_has_no_unowned_item(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path, 218)
    original = read.ledger_path.read_bytes()
    calls: list[str] = []
    result = HandlerCarryOver(
        append=lambda text: (calls.append(text) is None, "ok")
    ).handle(
        ModelCarryOverRequest(read=read, ask_ids=tuple(f"ask-{i}" for i in range(218)))
    )
    assert result.dry_run
    assert calls == []
    assert read.ledger_path.read_bytes() == original
    assert result.counts.total == result.counts.owned == 218
    assert len(result.text.splitlines()) == 1
    for count in ("218 owned", "0 done", "0 withdrawn", "0 parked"):
        assert count in result.text
    read.ledger_path.write_text("\n".join(result.proposed_rows) + "\n")
    replay = HandlerAskStateReport().handle(read)
    assert not replay.dropped
    assert all(
        e.owner_lane == "orchestrator" and e.state.value == "owned" for e in replay.asks
    )
    repeated = HandlerCarryOver().handle(
        ModelCarryOverRequest(read=read, ask_ids=tuple(f"ask-{i}" for i in range(218)))
    )
    assert repeated.proposed_rows == ()


def test_carry_over_effect_retry_does_not_duplicate_classifier_alarm(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path, 19)
    appended: list[str] = []
    capture_store.ingest(
        read.captures_dir,
        session_id="s",
        text="Check another item.",
        source="bus:test",
        received_at=NOW,
        origin_event="content-captured",
    )
    handler = HandlerCaptureProcess(
        lambda *_: DelegateAnswer(None, None, "no consumer"),
        lambda text: (appended.append(text) is None, "ok"),
        now=NOW,
    )
    result = handler.handle(
        ModelCaptureProcessRequest(
            store_dir=read.captures_dir, ledger_path=read.ledger_path
        )
    )
    assert len(result.findings) == 1
    assert sum("kind=finding" in text for text in appended) == 1
    capture_store.ingest(
        read.captures_dir,
        session_id="s",
        text="Check yet another item.",
        source="bus:test",
        received_at=NOW,
        origin_event="content-captured",
    )
    handler.handle(
        ModelCaptureProcessRequest(
            store_dir=read.captures_dir, ledger_path=read.ledger_path
        )
    )
    assert sum("kind=finding" in text for text in appended) == 1


def test_carry_over_cli_dry_run_prints_summary_and_writes_proposals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_ask_state_effect.runtime_ask_state import main

    read = sources(tmp_path)
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"ask_ids": [f"ask-{i}" for i in range(3)]}))
    out = tmp_path / "proposals.json"
    assert (
        main(
            [
                "--carry-over",
                str(cohort),
                "--dry-run",
                "--since",
                SINCE.isoformat(),
                "--now",
                NOW.isoformat(),
                "--ledger",
                str(read.ledger_path),
                "--captures-dir",
                str(read.captures_dir),
                "--out",
                str(out),
                "--no-archives",
            ]
        )
        == 0
    )
    assert "3 owned" in capsys.readouterr().out
    assert len(json.loads(out.read_text())["proposed_rows"]) == 3
    assert read.ledger_path.read_text() == ""


def test_carry_over_retains_parked_items_and_apply_uses_only_the_injected_port(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path)
    read.ledger_path.write_text(
        row("STATUS", "ask-0", "state=parked | reason=await decision") + "\n"
    )
    request = ModelCarryOverRequest(
        read=read, ask_ids=("ask-0", "ask-1", "ask-2"), dry_run=False
    )
    with pytest.raises(ValueError, match="accepted-ledger append port"):
        HandlerCarryOver().handle(request)
    calls: list[str] = []
    result = HandlerCarryOver(
        append=lambda text: (calls.append(text) is None, "accepted")
    ).handle(request)
    assert result.applied
    assert result.counts.parked == 1
    assert result.counts.owned == 2
    assert calls == ["\n".join(result.proposed_rows)]
    assert not any("ask=ask-0" in text for text in calls)
    assert "state=parked" in read.ledger_path.read_text()


def test_carry_over_refuses_foreign_ids_and_unconfirmed_or_injected_labels(
    tmp_path: Path,
) -> None:
    read = sources(tmp_path)
    with pytest.raises(ValueError, match="absent from source snapshot"):
        HandlerCarryOver().handle(
            ModelCarryOverRequest(read=read, ask_ids=("not-in-the-snapshot",))
        )
    with pytest.raises(ValueError, match="outside the cohort"):
        HandlerCarryOver().handle(
            ModelCarryOverRequest(
                read=read,
                ask_ids=("ask-0",),
                labels=(
                    ModelCarryOverLabel(
                        ask_id="ask-1",
                        label="not-an-ask",
                        run_id="run-a",
                        engine="second-engine",
                        confirmed_by="orchestrator",
                    ),
                ),
            )
        )
    with pytest.raises(ValueError, match="reference"):
        ModelCarryOverEvidence(ask_id="ask-0", reference="receipt:fake | state=done")


@pytest.mark.parametrize(
    "overlay_name", ["inmemory", pytest.param("redpanda", marks=pytest.mark.kafka)]
)
async def test_carry_over_runtime_uses_discovered_contract_and_overlay(
    tmp_path: Path,
    overlay_name: str,
) -> None:
    import asyncio
    import os
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
    from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
        read_node_event_bus,
    )
    from omnimarket.handlers.bus_binding.select import positional_opener
    from omnimarket.models.model_carry_over import ModelCarryOverResult
    from omnimarket.nodes import node_ask_state_effect

    overlay = read_node_event_bus(
        Path(__file__).parent / f"fixtures/overlays/{overlay_name}.yaml"
    )
    assert overlay is not None
    contract = yaml.safe_load(
        Path(node_ask_state_effect.__file__).with_name("contract.yaml").read_text()
    )
    contracts = tuple(
        c for c in discover_contracts().contracts if c.name == "node_ask_state_effect"
    )
    assert len(contracts) == 1
    read = sources(tmp_path)
    request = ModelCarryOverRequest(read=read, ask_ids=("ask-0", "ask-1", "ask-2"))
    received: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def collect(message: object) -> None:
        await received.put(json.loads(message.value))

    async with positional_opener(open_lab_run_bus)(
        overlay.type,
        overlay.lane,
        None,
        Path(os.environ["OMNI_HOME"]) if overlay.lane else None,
    ) as bus:
        await bus.subscribe(
            contract["terminal_event"],
            group_id="carry-over-results-" + str(uuid4()),
            on_message=collect,
        )
        engine = MessageDispatchEngine()
        wiring = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [r.outcome.value for r in wiring.results] == ["wired"]
        engine.freeze()
        topic = contract["runtime_dispatch"]["command_topic"]
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation,
            event_type=topic,
        )
        await bus.publish(topic, None, envelope.model_dump_json().encode(), None)
        event = await asyncio.wait_for(
            received.get(), timeout=60 if overlay.lane else 5
        )
        assert event["correlation_id"] == str(correlation)
        result = ModelCarryOverResult.model_validate(event["payload"])
        assert result.counts.owned == 3
        assert result.dry_run
        assert read.ledger_path.read_text() == ""
