# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host state checks reject restored session accounts and forged install claims."""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omnimarket.models.host_checks import (
    ModelHostAccountRequest,
    ModelHostCheckPolicy,
    ModelHostCheckSnapshot,
    ModelHostCheckTick,
    ModelHostFact,
    ModelInstallClaim,
    ModelInstallClaimRequest,
)
from omnimarket.nodes.node_host_account_check_compute import HandlerHostAccountCheck
from omnimarket.nodes.node_host_account_install_claim_effect.handlers.handler_host_account_install_claim import (
    HandlerHostAccountInstallClaim,
)
from omnimarket.nodes.node_host_account_install_claim_effect.handlers.host_check_events import (
    MemoryHostCheckEvents,
)
from omnimarket.nodes.node_host_account_install_claim_effect.handlers.host_check_facts import (
    HostCheckFacts,
    MemoryHostCheckFacts,
    account_label,
    installed_claims,
)
from omnimarket.nodes.node_host_account_install_claim_effect.handlers.host_check_journal import (
    MemoryHostCheckJournal,
    SqliteHostCheckJournal,
)
from omnimarket.nodes.node_install_claim_check_compute import HandlerInstallClaimCheck

NOW = datetime(2026, 11, 4, 19, 20, tzinfo=UTC)
PACKAGE = "omnimarket.nodes.node_host_account_install_claim_effect"


def policy() -> ModelHostCheckPolicy:
    return ModelHostCheckPolicy.model_validate(
        yaml.safe_load(
            files(PACKAGE).joinpath("pool_accounts.example.yaml").read_text()
        )
    )


def snapshot(label: str | None = "session-own") -> ModelHostCheckSnapshot:
    return ModelHostCheckSnapshot(
        hosts=(
            ModelHostFact(
                host="lab",
                account_label=label,
                installed_revision="b" * 40,
                contains={"a" * 40: False},
            ),
        ),
        claims=(
            ModelInstallClaim(
                row_id="recorded-false-claim",
                host="lab",
                sha="a" * 40,
                writer_principal="lane-service",
                actor="install-service",
            ),
        ),
        ledger_readable=True,
    )


@pytest.fixture(params=["inmemory", "durable"])
def effect(request: pytest.FixtureRequest, tmp_path: Path):
    journal = (
        MemoryHostCheckJournal()
        if request.param == "inmemory"
        else SqliteHostCheckJournal(str(tmp_path / "journal.db"), timeout_seconds=30)
    )
    facts, events = (
        MemoryHostCheckFacts(snapshot()),
        MemoryHostCheckEvents(principal=policy().check_principal),
    )
    handler = HandlerHostAccountInstallClaim(
        policy=policy(), facts=facts, journal=journal, events=events
    )
    return handler, journal, facts, events


@pytest.mark.parametrize(
    ("label", "expected"),
    [("pool-a", "OK"), ("session-own", "FINDING"), (None, "UNREAD"), ("", "UNREAD")],
)
def test_host_account_install_claim_account(label: str | None, expected: str) -> None:
    result = HandlerHostAccountCheck().handle(
        ModelHostAccountRequest(
            host=ModelHostFact(host="lab", account_label=label),
            pool_accounts=("pool-a",),
            token_switch_fix="Follow the approved token-switch procedure; ask the credential owner.",
        )
    )
    assert result.verdict == expected
    assert result.account_label == label
    assert result.check == "S7"


@pytest.mark.parametrize("actor", ["lane", "install-workflow"])
def test_host_account_install_claim_forged_actor(actor: str) -> None:
    result = HandlerInstallClaimCheck().handle(
        ModelInstallClaimRequest(
            claim=ModelInstallClaim(
                row_id="claim-1",
                host="lab",
                sha="a" * 40,
                writer_principal="lane-service",
                actor=actor,
            ),
            host=ModelHostFact(
                host="lab", installed_revision="b" * 40, contains={"a" * 40: False}
            ),
            install_principals=("install-service",),
        )
    )
    assert result.verdict == "FINDING"
    assert result.reason == "claimed-installed-but-absent"
    assert not result.accepted


def test_host_account_install_claim_workflow_readback() -> None:
    result = HandlerInstallClaimCheck().handle(
        ModelInstallClaimRequest(
            claim=ModelInstallClaim(
                row_id="claim-2",
                host="lab",
                sha="a" * 40,
                writer_principal="install-service",
            ),
            host=ModelHostFact(
                host="lab", installed_revision="b" * 40, contains={"a" * 40: True}
            ),
            install_principals=("install-service",),
        )
    )
    assert result.verdict == "OK"
    assert result.accepted


def test_host_account_install_claim_persisting_breaks_escalate_once(effect) -> None:
    handler, journal, _, events = effect
    for index in range(3):
        result = handler.handle(
            ModelHostCheckTick(tick_id=str(index), at=NOW + timedelta(minutes=index))
        )
        assert len(result.cursors) == 2
        assert {c.check for c in result.cursors} == {"S7", "S9"}
        assert {c.verdict for c in result.cursors} == {"FINDING"}
        assert result.exceptions_added == (2 if index == 0 else 0)
        assert not result.error
    exceptions = [e for e in events.events if e.key.startswith("exception:")]
    assert len(exceptions) == 2
    assert {event.topic for event in events.events} == {
        "onex.cmd.omnimarket.exception-emit-requested.v1",
        "onex.evt.omnimarket.host-check-cursor-recorded.v1",
        "onex.evt.omnimarket.automation-run-observed.v1",
    }
    assert all(e.payload["runner"] is None for e in exceptions)
    assert all("no repair attempted" in e.payload["row"]["tried"] for e in exceptions)
    assert exceptions[0].payload["row"]["proposed_fix"] == policy().token_switch_fix
    assert len([e for e in events.events if e.key.startswith("cursor:")]) == 6
    if isinstance(journal, SqliteHostCheckJournal):
        with sqlite3.connect(journal.path) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM host_check_cursors").fetchone()[0] == 6
            )


def test_host_account_install_claim_restart_and_replay(effect) -> None:
    handler, journal, facts, events = effect
    tick = ModelHostCheckTick(tick_id="replay", at=NOW)
    first = handler.handle(tick)
    if isinstance(journal, SqliteHostCheckJournal):
        journal = SqliteHostCheckJournal(journal.path, journal.timeout_seconds)
    restarted = HandlerHostAccountInstallClaim(
        policy=policy(), facts=facts, journal=journal, events=events
    )
    assert restarted.handle(tick) == first
    later = restarted.handle(tick.model_copy(update={"tick_id": "later"}))
    assert later.exceptions_added == 0
    assert len([e for e in events.events if e.key.startswith("exception:")]) == 2


def test_host_account_install_claim_broker_failure_retries_outbox(effect) -> None:
    handler, journal, facts, events = effect

    class Unavailable:
        principal = policy().check_principal

        def publish(self, event) -> None:
            raise OSError("broker offline")

    broken = HandlerHostAccountInstallClaim(
        policy=policy(), facts=facts, journal=journal, events=Unavailable()
    )
    tick = ModelHostCheckTick(tick_id="outbox", at=NOW)
    result = broken.handle(tick)
    assert result.pending_events == 6
    assert len(result.cursors) == 2
    assert result.error == "host-check outbox delivery pending"
    assert handler.handle(tick).pending_events == 0
    assert not journal.pending()
    assert len(events.events) == 6


@pytest.mark.parametrize("row_type", ["STATUS", "TERMINAL"])
def test_host_account_install_claim_only_broker_stamp_counts(row_type: str) -> None:
    text = (
        f"2026-11-04T19:20:00Z | {row_type} | lane=lane | principal=lane-service | "
        f"actor=install-service | installed=lab:{'a' * 40} | observed=unit ran"
    )
    (claim,) = installed_claims(text)
    assert claim.writer_principal == "lane-service"
    assert claim.actor == "install-service"
    assert claim.row_type == row_type
    result = HandlerInstallClaimCheck().handle(
        ModelInstallClaimRequest(
            claim=claim,
            host=ModelHostFact(
                host="lab", installed_revision="a" * 40, contains={"a" * 40: True}
            ),
            install_principals=("install-service",),
        )
    )
    assert result.verdict == "FINDING"
    assert not result.accepted
    spoofed = text.replace(
        "principal=lane-service", "principal=lane-service | principal=install-service"
    )
    assert installed_claims(spoofed)[0].writer_principal is None
    assert (
        installed_claims(text.replace(" | installed=", " | free text installed=")) == ()
    )


@pytest.mark.parametrize(
    ("contained", "revision", "expected"),
    [(False, "b" * 40, "FINDING"), (None, None, "UNREAD"), (True, "b" * 40, "OK")],
)
def test_host_account_install_claim_workflow_requires_host_read(
    contained, revision, expected
) -> None:
    claim = ModelInstallClaim(
        row_id="readback", host="lab", sha="a" * 40, writer_principal="install-service"
    )
    result = HandlerInstallClaimCheck().handle(
        ModelInstallClaimRequest(
            claim=claim,
            host=ModelHostFact(
                host="lab", installed_revision=revision, contains={claim.sha: contained}
            ),
            install_principals=("install-service",),
        )
    )
    assert result.verdict == expected
    assert result.accepted == (expected == "OK")


def test_host_account_install_claim_auth_read_emits_label_only() -> None:
    import hashlib

    def hashed(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:10]

    data = json.loads(policy().model_dump_json())
    data["account_identities"] = [
        {
            "label": "pool-a",
            "org_hash": hashed("org"),
            "email_hash": hashed("pool@example.invalid"),
        }
    ]
    configured = ModelHostCheckPolicy.model_validate(data)
    raw = json.dumps(
        {
            "loggedIn": True,
            "orgId": "org",
            "email": "pool@example.invalid",
            "accessToken": "secret-must-not-leave-auth-reader",
        }
    )
    assert account_label(raw, configured) == "pool-a"
    unknown = account_label(
        raw.replace("pool@example.invalid", "session@example.invalid"), configured
    )
    assert unknown is not None
    assert unknown.startswith("unlisted-")
    assert "secret" not in unknown
    assert "@" not in unknown
    assert account_label('{"loggedIn": false}', configured) is None


@pytest.mark.parametrize(
    "principals",
    [
        None,
        {
            "lane_principals": ["install-service"],
            "install_principals": ["install-service"],
            "check_principal": "goal-check-service",
        },
    ],
)
def test_host_account_install_claim_r8_shared_or_missing_is_loud(
    principals, tmp_path: Path
) -> None:
    configured = policy().model_copy(update={"ledger_path": str(tmp_path / "ledger")})
    reads = []

    def run(argv, **kwargs):
        reads.append(argv)
        if argv == configured.hosts[0].principals_read:
            return subprocess.CompletedProcess(
                argv,
                1 if principals is None else 0,
                json.dumps(principals),
                "hidden secret",
            )
        return subprocess.CompletedProcess(argv, 1, "hidden secret", "hidden secret")

    journal = MemoryHostCheckJournal()
    handler = HandlerHostAccountInstallClaim(
        policy=configured,
        facts=HostCheckFacts(),
        journal=journal,
        events=MemoryHostCheckEvents(principal=policy().check_principal),
    )
    with patch("subprocess.run", side_effect=run):
        result = handler.handle(ModelHostCheckTick(tick_id="r8", at=NOW))
    assert "R8" in result.error
    assert {c.verdict for c in result.cursors} == {"UNREAD"}
    assert journal.result("r8") is not None
    assert "hidden secret" not in result.model_dump_json()
    assert set(reads) <= {
        configured.hosts[0].auth_read,
        configured.hosts[0].revision_read,
        configured.hosts[0].principals_read,
    }


def test_host_account_install_claim_real_read_adapter(tmp_path: Path) -> None:
    configured = policy().model_copy(update={"ledger_path": str(tmp_path / "ledger")})
    Path(configured.ledger_path).write_text(
        f"2026-11-04T19:20:00Z | STATUS | principal=lane-service | actor=install-service | installed=lab:{'a' * 40} | observed=ran\n"
    )
    identity = {
        "lane_principals": ["lane-service"],
        "install_principals": ["install-service"],
        "check_principal": "goal-check-service",
    }
    called = []

    def run(argv, **kwargs):
        called.append(argv)
        assert kwargs["capture_output"]
        assert not kwargs["check"]
        binding = configured.hosts[0]
        output = (
            json.dumps(identity)
            if argv == binding.principals_read
            else (
                json.dumps(
                    {
                        "loggedIn": True,
                        "orgId": "org",
                        "email": "session@example.invalid",
                        "accessToken": "secret",
                    }
                )
                if argv == binding.auth_read
                else "b" * 40
            )
        )
        code = 1 if "--is-ancestor" in argv else 0
        return subprocess.CompletedProcess(argv, code, output, "secret")

    with patch("subprocess.run", side_effect=run):
        read = HostCheckFacts().read(configured)
    assert not read.prerequisite_error
    assert read.ledger_readable
    assert read.hosts[0].account_label.startswith("unlisted-")
    assert read.hosts[0].contains == {"a" * 40: False}
    assert len(called) == 4
    assert "secret" not in read.model_dump_json()


def test_host_account_install_claim_stopped_cursor_is_missed(effect) -> None:
    from omnimarket.models.liveness.model_automation_liveness import (
        ModelAutomationRunObserved,
    )
    from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_verdicts import (
        evaluate_process,
    )
    from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
        ModelProcessRunFacts,
        ModelRunFact,
    )

    handler, _, _, events = effect
    handler.handle(ModelHostCheckTick(tick_id="last", at=NOW))
    for check in ("S7", "S9"):
        event = next(e for e in events.events if e.key == f"liveness:{check}:last")
        run = ModelAutomationRunObserved.model_validate(event.payload)
        entry = policy().liveness_entries[check]
        fact = ModelProcessRunFacts(
            process_id=entry.process_id,
            host=entry.host,
            declared_at=NOW,
            last_run_at=NOW,
            runs=(
                ModelRunFact(
                    run_id=run.run_id,
                    started_at=NOW,
                    finished_at=NOW,
                    outcome="ok",
                    did_work_count=run.did_work_count,
                ),
            ),
        )
        assert evaluate_process(entry, fact, NOW).verdict.value == "healthy"
        assert (
            evaluate_process(entry, fact, NOW + timedelta(seconds=121)).verdict.value
            == "missed"
        )


def test_host_account_install_claim_public_handler() -> None:
    from omnimarket.nodes.node_host_account_install_claim_effect import (
        HandlerHostAccountInstallClaim as PublicHandler,
    )

    assert PublicHandler is HandlerHostAccountInstallClaim


def test_host_account_install_claim_overlay_binds_all_ports() -> None:
    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        bind_overlay_file,
    )

    try:
        bind_overlay_file(str(files(PACKAGE).joinpath("overlay.inmemory.yaml")))
        result = HandlerHostAccountInstallClaim().handle(
            ModelHostCheckTick(tick_id="bound", at=NOW)
        )
        assert not result.error
        assert result.exceptions_added == 1
        assert {f.check: f.verdict for f in result.findings} == {
            "S7": "OK",
            "S9": "FINDING",
        }
    finally:
        bind_overlay(None)


@pytest.mark.parametrize(
    ("name", "completed", "payload", "expected"),
    [
        (
            "node_host_account_install_claim_effect",
            "onex.evt.omnimarket.host-account-install-claim-effect-completed.v1",
            ModelHostCheckTick(tick_id="runtime", at=NOW).model_dump(mode="json"),
            {"exceptions_added": 1},
        ),
        (
            "node_host_account_check_compute",
            "onex.evt.omnimarket.host-account-check-compute-completed.v1",
            ModelHostAccountRequest(
                host=ModelHostFact(host="lab", account_label="session-own"),
                pool_accounts=("pool-a",),
                token_switch_fix="Ask the credential owner.",
            ).model_dump(mode="json"),
            {"check": "S7", "verdict": "FINDING"},
        ),
        (
            "node_install_claim_check_compute",
            "onex.evt.omnimarket.install-claim-check-compute-completed.v1",
            ModelInstallClaimRequest(
                claim=snapshot().claims[0],
                host=snapshot().hosts[0],
                install_principals=("install-service",),
            ).model_dump(mode="json"),
            {"check": "S9", "verdict": "FINDING", "accepted": False},
        ),
    ],
)
async def test_host_account_install_claim_installed_runtime_bus_roundtrip(
    name: str, completed: str, payload: dict, expected: dict
) -> None:
    """Each packaged node responds on its declared topic through the real runtime."""
    import asyncio
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        bind_overlay_file,
    )

    contracts = tuple(c for c in discover_contracts().contracts if c.name == name)
    assert len(contracts) == 1
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    command = contract["runtime_dispatch"]["command_topic"]
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == completed
    bus = EventBusInmemory(environment="local", group="host-state-proof")
    answers = asyncio.Queue()

    async def collect(message):
        await answers.put(json.loads(message.value))

    await bus.start()
    try:
        bind_overlay_file(str(files(PACKAGE).joinpath("overlay.inmemory.yaml")))
        await bus.subscribe(
            completed, group_id="host-state-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            name: "wired"
        }
        engine.freeze()
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=payload,
            correlation_id=correlation,
            event_type="host-state-tick",
        )
        await bus.publish(command, None, envelope.model_dump_json().encode(), None)
        answer = await asyncio.wait_for(answers.get(), timeout=5)
        assert answer["correlation_id"] == str(correlation)
        for key, value in expected.items():
            assert answer["payload"][key] == value
    finally:
        bind_overlay(None)
        await bus.close()


def test_host_account_install_claim_check_principal() -> None:
    events = MemoryHostCheckEvents(principal=policy().check_principal)
    events.principal = "lane-service"
    journal = MemoryHostCheckJournal()
    handler = HandlerHostAccountInstallClaim(
        policy=policy(),
        facts=MemoryHostCheckFacts(snapshot()),
        journal=journal,
        events=events,
    )
    result = handler.handle(ModelHostCheckTick(tick_id="wrong-service", at=NOW))
    assert "R8" in result.error
    assert {c.verdict for c in result.cursors} == {"UNREAD"}
    assert not events.events
    assert journal.result("wrong-service") is not None


def test_host_account_install_claim_pool_overlay_reread(tmp_path: Path) -> None:
    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        bind_overlay_file,
    )

    declaration = yaml.safe_load(
        files(PACKAGE).joinpath("overlay.inmemory.yaml").read_text()
    )
    pool = tmp_path / "pool.yaml"
    pool.write_text(yaml.safe_dump(policy().model_dump(mode="json")))
    declaration["dependencies"][0]["implementation"] = "file_overlay_documents"
    declaration["dependencies"][0]["config"] = {"host_checks": str(pool)}
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(declaration))
    try:
        bind_overlay_file(str(overlay))
        handler = HandlerHostAccountInstallClaim()
        first = handler.handle(ModelHostCheckTick(tick_id="before", at=NOW))
        assert first.cursors[0].verdict == "OK"
        changed = policy().model_copy(update={"pool_accounts": ("other-pool",)})
        pool.write_text(yaml.safe_dump(changed.model_dump(mode="json")))
        second = handler.handle(ModelHostCheckTick(tick_id="after", at=NOW))
        assert second.cursors[0].verdict == "FINDING"
    finally:
        bind_overlay(None)


def test_host_account_install_claim_queue_idempotency(effect, tmp_path: Path) -> None:
    """The actual queue consumes outbox commands and repairs no credential."""
    from dataclasses import replace

    from pydantic import TypeAdapter

    from omnimarket.models.exception_queue import (
        ModelExceptionEmitRequest,
        ModelExceptionQueueRequest,
    )
    from omnimarket.nodes.node_exception_queue_effect.node import (
        NodeExceptionQueueEffect,
    )

    handler, _, _, events = effect
    handler.handle(ModelHostCheckTick(tick_id="queue", at=NOW))
    queue = NodeExceptionQueueEffect()
    queued = [e for e in events.events if e.key.startswith("exception:")]
    for event in queued:
        request = TypeAdapter(ModelExceptionEmitRequest).validate_python(event.payload)
        request = replace(request, state_dir=tmp_path / "queue")
        first = queue.handle(request)
        second = queue.handle(request)
        assert first.status == "appended"
        assert second.status == "repeat"
        assert not first.mechanize.startswith("sent:")
    result = queue.handle(
        ModelExceptionQueueRequest(
            state_dir=tmp_path / "queue", audience="orchestrator"
        )
    )
    assert len(result.rows) == 2
    assert {row["cls"] for row in result.rows} == {
        "host-account-outside-pool",
        "claimed-installed-but-absent",
    }


def test_host_account_install_claim_append_service_stamps_writer(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from omnimarket.nodes.node_work_ledger_append_effect import (
        HandlerWorkLedgerAppendEffect,
        ModelWorkLedgerAppendRequest,
    )
    from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
        LocalLedgerFile,
        ModelAppendCommandResult,
    )

    ledger = tmp_path / "ledger.md"
    ledger.write_text("")

    class Runner:
        def append(self, rows: str):
            with ledger.open("a") as handle:
                handle.write(rows + "\n")
            return ModelAppendCommandResult(exit_code=0, stderr="")

    appender = HandlerWorkLedgerAppendEffect(
        Runner(),
        LocalLedgerFile(ledger),
        "lab",
        writer_principals=("lane-service", "install-service"),
    )
    for principal in ("lane-service", "install-service"):
        request_id = uuid4()
        row = f"2026-11-04T19:20:00Z | STATUS | lane=fixture | actor=install-service | req={request_id} | installed=lab:{'a' * 40} | observed=unit ran"
        receipt = appender.handle(
            ModelWorkLedgerAppendRequest(
                request_id=request_id,
                rows=row,
                requested_by_lane="fixture",
                requesting_host="lab",
                requested_at=NOW,
            ),
            writer_principal=principal,
        )
        assert receipt.exit_code == 0
    claims = installed_claims(ledger.read_text())
    assert len(claims) == 2
    assert [claim.writer_principal for claim in claims] == [
        "lane-service",
        "install-service",
    ]
    host = ModelHostFact(
        host="lab", installed_revision="b" * 40, contains={"a" * 40: True}
    )
    results = [
        HandlerInstallClaimCheck().handle(
            ModelInstallClaimRequest(
                claim=claim, host=host, install_principals=("install-service",)
            )
        )
        for claim in claims
    ]
    assert [result.accepted for result in results] == [False, True]


def test_host_account_install_claim_journal_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections = []
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        was_closed = False

        def close(self) -> None:
            self.was_closed = True
            super().close()

    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked)
    journal = SqliteHostCheckJournal(str(tmp_path / "closed.db"), timeout_seconds=30)
    assert journal.result("absent") is None
    observed = [connection.was_closed for connection in connections]
    for connection in connections:
        connection.close()
    assert len(observed) == 2
    assert all(observed)


@pytest.mark.parametrize("acknowledgement", ["ok", "error", "pending", "buffer-full"])
def test_host_account_install_claim_redpanda_port(
    acknowledgement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import confluent_kafka

    from omnimarket.handlers.container.container_overlay_dependencies import (
        load_overlay_bindings,
        require_dependencies,
    )
    from omnimarket.models.host_checks import ModelHostCheckCursor, ModelHostCheckEvent
    from omnimarket.nodes.node_host_account_install_claim_effect.handlers.host_check_events import (
        KafkaHostCheckEvents,
    )
    from omnimarket.protocols.protocol_host_checks import ProtocolHostCheckEvents

    produced = []
    configurations = []

    class Producer:
        def __init__(self, config):
            configurations.append(config)

        def produce(self, topic, **kwargs):
            if acknowledgement == "buffer-full":
                raise BufferError("fixture-password")
            produced.append((topic, kwargs))

        def flush(self, timeout):
            assert timeout == 30
            if acknowledgement != "pending":
                produced[-1][1]["on_delivery"](
                    None
                    if acknowledgement == "ok"
                    else RuntimeError("fixture-password"),
                    None,
                )
            return 0 if acknowledgement == "ok" else 1

    monkeypatch.setattr(confluent_kafka, "Producer", Producer)
    monkeypatch.setenv("GOAL_CHECK_SASL_USERNAME", policy().check_principal)
    monkeypatch.setenv("GOAL_CHECK_SASL_PASSWORD", "fixture-password")
    overlay = load_overlay_bindings(
        Path(str(files(PACKAGE).joinpath("overlay.redpanda.yaml")))
    )
    protocol = (
        f"{ProtocolHostCheckEvents.__module__}.{ProtocolHostCheckEvents.__name__}"
    )
    sink = require_dependencies((protocol,), overlay)[protocol]
    assert isinstance(sink, KafkaHostCheckEvents)
    assert sink.principal == policy().check_principal
    assert configurations[0]["sasl.password"] == "fixture-password"
    topic = yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text())[
        "host_check_topics"
    ]["cursor"]
    cursor = ModelHostCheckCursor(
        check="S7", tick_id="redpanda", at=NOW, verdict="OK", rows_read=1
    )
    event = ModelHostCheckEvent(
        key="cursor:S7:redpanda", topic=topic, payload=cursor.model_dump(mode="json")
    )
    if acknowledgement != "ok":
        with pytest.raises(OSError, match="was not acknowledged") as caught:
            sink.publish(event)
        assert "fixture-password" not in str(caught.value)
    else:
        sink.publish(event)
        assert len(produced) == 1
        assert produced[0][0] == topic
        envelope = json.loads(produced[0][1]["value"])
        assert envelope["payload"] == cursor.model_dump(mode="json")
