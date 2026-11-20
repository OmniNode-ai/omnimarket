# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the red-main alarm on the runtime tick with recorded SCM results and the declared owner map."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring import (
    discover_contracts_from_paths,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.ledger_row_grammar import (
    GOVERNED_LEDGER_NAMES,
    refusal_for_payload,
)
from omnimarket.handlers.scm.handler_scm_local_state import WATCHER_SCHEMA
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire
from omnimarket.models.overlay_documents.model_registry_documents import (
    ModelRepositoryRegistryOverlay,
)
from omnimarket.models.work_ledger_append import (
    EnumLedgerAppendMode,
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    ModelWorkLedgerOpenClaim,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_red_main_alarm_effect import ports as ports_module
from omnimarket.nodes.node_red_main_alarm_effect.handler import (
    HandlerRedMainAlarm,
    HandlerRedMainAlarmScheduled,
    schedule_config,
)
from omnimarket.nodes.node_red_main_alarm_effect.inventory import read_inventory
from omnimarket.nodes.node_red_main_alarm_effect.models import (
    EnumAlarmFailure,
    ModelAlarmInventory,
    ModelAlarmRepository,
    ModelAlarmSettings,
    ModelAlarmTarget,
    ModelRedMainAlarmResult,
)
from omnimarket.nodes.node_red_main_alarm_effect.ports import AlarmBusPorts
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    InMemoryBusLedgerStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers import LedgerFlipFence
from omnimarket.nodes.node_work_ledger_append_effect.protocols import LocalLedgerFile

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
NOW = datetime(2026, 11, 19, 3, 40, tzinfo=UTC)
PARENT = "orchestrator-31d9-swarm"
# An epoch-aligned 15-minute window start: the runtime tick that opens it carries the scan.
WINDOW = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
SETTINGS = ModelAlarmSettings(parent_lane=PARENT, host="h053")
FIXTURES = Path(__file__).with_name("fixtures")


def recorded(name: str) -> ModelCommitCheckRunsResult:
    raw = json.loads((FIXTURES / "commit_check_runs.json").read_text())
    return ModelCommitCheckRunsResult.model_validate(raw[name])


def tick(now: datetime = NOW) -> ModelScheduledFire:
    return ModelScheduledFire(
        workflow="red-main-alarm",
        fire_id=f"red-main-alarm-{now:%Y%m%dT%H%M%SZ}",
        window_start=now,
        interval_seconds=900,
        tick_id=uuid4(),
        tick_now=now,
        scheduler_id="test",
    )


def claim(*cells: str) -> ModelWorkLedgerOpenClaim:
    row = " | ".join(
        (NOW.isoformat(), "CLAIM", "lane=repair-owner", "ticket=OMN-25860", *cells)
    )
    return ModelWorkLedgerOpenClaim(
        lane="repair-owner",
        claim=ModelWorkLedgerRowRecord(
            row_id="test-claim",
            row_ts=NOW,
            row_type="CLAIM",
            row_lane="repair-owner",
            text=row,
            raw_row=row,
            source="test",
        ),
        ticket="OMN-25860",
        newest_row_stamp=NOW.isoformat(),
        newest_row_kind="CLAIM",
        rows=1,
        host="",
        run="",
        leases=(),
    )


class LedgerAndScm:
    def __init__(self, answer: str = "red") -> None:
        self.answer = recorded(answer)
        self.target = ModelAlarmTarget(
            repo=self.answer.repo,
            branch="main",
            head_sha=self.answer.head_sha,
            required_checks=("ci",),
        )
        self.claims: tuple[ModelWorkLedgerOpenClaim, ...] = ()
        self.requests: dict[UUID, ModelWorkLedgerAppendRequest] = {}
        self.fail_check = False

    def move_to(self, answer: str) -> None:
        self.answer = recorded(answer)
        self.target = self.target.model_copy(update={"head_sha": self.answer.head_sha})

    async def inventory(self, now: datetime) -> ModelAlarmInventory:
        return ModelAlarmInventory(targets=(self.target,))

    async def check_runs(self, target: ModelAlarmTarget) -> ModelCommitCheckRunsResult:
        if self.fail_check:
            raise OSError("gateway unavailable")
        return self.answer

    async def open_claims(self, now: datetime) -> tuple[ModelWorkLedgerOpenClaim, ...]:
        return self.claims

    async def recorded(self, request_id: UUID) -> bool:
        return request_id in self.requests

    async def append(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        assert request.request_id not in self.requests
        self.requests[request.request_id] = request
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=EnumWorkLedgerAppendStatus.ACCEPTED,
            exit_code=0,
            message="",
            ledger_lines=[1, 2],
            ledger_host="test",
            duration_ms=0,
        )


@pytest.mark.parametrize(
    ("cells", "target_fields"),
    [
        (("cause=fixture-core:ci",), {}),
        (("pr=fixture-core#1604",), {"related_prs": ("fixture-core#1604",)}),
        ((), {"related_tickets": ("OMN-25860",)}),
    ],
)
async def test_red_main_alarm_uses_live_claim(
    cells: tuple[str, ...], target_fields: dict[str, tuple[str, ...]]
) -> None:
    ports = LedgerAndScm()
    ports.claims = (claim(*cells),)
    ports.target = ports.target.model_copy(
        update={"owner": "repo-owner", **target_fields}
    )
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    result = await handler.handle(tick())

    assert not result.failures
    assert len(result.alarms) == 1
    assert result.alarms[0].owner == "repair-owner"
    assert result.alarms[0].owner_source == "claim"
    (request,) = ports.requests.values()
    alarm, message = request.rows.splitlines()
    assert " | STATUS | " in alarm
    assert " | src=alarm | alarm=main-red | " in alarm
    assert " | actor=node:node_red_main_alarm_effect | model=none | " in alarm
    assert " | host=h053 | " in alarm
    assert " | drill=yes" not in alarm
    assert f" | repo={ports.target.repo} | " in alarm
    assert " | branch=main | " in alarm
    assert f" | head={ports.target.head_sha} | " in alarm
    assert " | check=ci | conclusion=failure | " in alarm
    assert " | first_red_at=2026-11-19T03:40:00Z | " in alarm
    assert " | owner=repair-owner | owner_source=claim | " in alarm
    assert " | MSG | " in message
    assert " | to=repair-owner | " in message
    assert f" | head={ports.target.head_sha} | " in message


@pytest.mark.parametrize(
    ("registry_owner", "mapped_owner", "expected_owner", "source"),
    [
        (None, None, PARENT, "none"),
        ("repo-owner", None, "repo-owner", "registry"),
        (None, "mapped-owner", "mapped-owner", "declared"),
        ("repo-owner", "mapped-owner", "repo-owner", "registry"),
    ],
)
async def test_red_main_alarm_falls_back_to_named_owner(
    registry_owner: str | None,
    mapped_owner: str | None,
    expected_owner: str,
    source: str,
) -> None:
    ports = LedgerAndScm()
    ports.target = ports.target.model_copy(
        update={"owner": registry_owner, "declared_owner": mapped_owner}
    )
    ports.claims = (claim("cause=unrelated:ci"),)
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    result = await handler.handle(tick())

    assert not result.failures
    (alarm,) = result.alarms
    assert alarm.owner == expected_owner
    assert alarm.owner_source == source
    (request,) = ports.requests.values()
    row, message = request.rows.splitlines()
    assert f" | owner={expected_owner} | owner_source={source} | " in row
    assert f" | to={expected_owner} | " in message


async def test_red_main_alarm_row_matches_the_ledger_alarm_grep() -> None:
    """The row an owner reads carries the src=alarm and alarm=main-red cells the ledger grep names."""
    ports = LedgerAndScm()
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    await handler.handle(tick())

    (request,) = ports.requests.values()
    status = request.rows.splitlines()[0]
    assert re.search(r"src=alarm.*alarm=main-red", status)
    # The ledger refused the MSG id this node wrote on 2026-11-19: an id is the row's own
    # timestamp and the sender's lane, and nothing else.
    assert refusal_for_payload(request.rows, sorted(GOVERNED_LEDGER_NAMES)[0]) is None
    assert " | id=2026-11-19T03:40:00Z-red-main-alarm | " in request.rows
    assert re.search(r" \| owner=[^ |]+ \| ", status)


async def test_red_main_drill_scans_only_named_repositories_and_marks_rows() -> None:
    ports = LedgerAndScm()
    drill = SETTINGS.model_copy(update={"drill_repos": (ports.target.repo,)})
    handler = HandlerRedMainAlarm(ports=ports, settings=drill, clock=lambda: NOW)

    result = await handler.handle(tick())

    assert len(result.alarms) == 1
    (request,) = ports.requests.values()
    status, message = request.rows.splitlines()
    assert " | drill=yes | " in status
    assert " | drill=yes | " in message

    elsewhere = LedgerAndScm()
    other = SETTINGS.model_copy(update={"drill_repos": ("example/elsewhere",)})
    quiet = await HandlerRedMainAlarm(
        ports=elsewhere, settings=other, clock=lambda: NOW
    ).handle(tick())
    assert not quiet.alarms
    assert not elsewhere.requests


async def test_red_main_alarm_deduplicates_across_handler_restarts() -> None:
    ports = LedgerAndScm()
    first = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)
    assert len((await first.handle(tick())).alarms) == 1

    later = NOW + timedelta(minutes=10)
    ports.claims = (claim("cause=fixture-core:ci"),)
    second = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: later)
    duplicate = await second.handle(tick(later))

    assert not duplicate.alarms
    assert not duplicate.failures
    assert len(ports.requests) == 1
    assert len(next(iter(ports.requests.values())).rows.splitlines()) == 2


@pytest.mark.parametrize("answer", ["green", "green_rerun"])
async def test_red_main_alarm_stays_quiet_on_green(answer: str) -> None:
    ports = LedgerAndScm(answer)
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    result = await handler.handle(tick())

    assert not result.alarms
    assert not result.failures
    assert not ports.requests


async def test_red_main_alarm_red_green_other_red() -> None:
    ports = LedgerAndScm()
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    first = await handler.handle(tick())
    ports.move_to("green")
    green = await handler.handle(tick(NOW + timedelta(minutes=10)))
    ports.move_to("other_red")
    last = await handler.handle(tick(NOW + timedelta(minutes=20)))

    assert len(first.alarms) == len(last.alarms) == 1
    assert not green.alarms
    assert not first.failures
    assert not green.failures
    assert not last.failures
    assert first.alarms[0].head_sha != last.alarms[0].head_sha
    assert len(ports.requests) == 2


async def test_red_main_alarm_check_read_failure_is_typed_and_quiet() -> None:
    ports = LedgerAndScm()
    ports.fail_check = True
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)

    result = await handler.handle(tick())

    assert not result.alarms
    assert not ports.requests
    (failure,) = result.failures
    assert failure.kind == EnumAlarmFailure.CHECK_RUNS_UNREADABLE
    assert failure.repo == ports.target.repo
    assert "gateway unavailable" in failure.detail


async def test_red_main_alarm_contract_declares_its_own_fifteen_minute_schedule() -> (
    None
):
    node_root = Path(__file__).parents[3] / "src/omnimarket/nodes"
    alarm = yaml.safe_load(
        (node_root / "node_red_main_alarm_effect/contract.yaml").read_text()
    )
    [subscription] = alarm["input_subscriptions"]
    [route] = alarm["handler_routing"]["handlers"]
    assert subscription == {
        "topic": "onex.intent.platform.runtime-tick.v1",
        "operation": "red_main_alarm.scheduled_run",
    }
    assert subscription == {"topic": route["topic"], "operation": route["operation"]}
    assert route["event_model"].endswith(".ModelRuntimeTick")
    assert alarm["event_bus"]["subscribe_topics"] == [subscription["topic"]]
    config = schedule_config()
    assert (config.workflow, config.interval_seconds) == ("red-main-alarm", 900)


async def test_red_main_alarm_inventory_uses_declared_default_branches() -> None:
    watcher = FIXTURES / "watcher_state.json"
    last_tick = datetime.fromisoformat(json.loads(watcher.read_text())["last_tick"])
    registry = ModelRepositoryRegistryOverlay.model_validate(
        {
            "repositories": [
                {
                    "name": "omnimarket",
                    "repo": "OmniNode-ai/omnimarket",
                    "type": "python",
                    "description": "market nodes",
                    "owner": "registry-owner",
                }
            ]
        }
    )

    inventory = read_inventory(
        watcher, last_tick + timedelta(seconds=1), scm_owner="", registry=registry
    )

    assert not inventory.failures
    target_by_repo = {target.repo: target for target in inventory.targets}
    main = target_by_repo["OmniNode-ai/fixture-core"]
    assert main.branch == "main"
    assert main.required_checks == ("ci",)
    assert main.owner is None
    assert main.related_prs == ("fixture-core#1604",)
    dev = target_by_repo["OmniNode-ai/omnimarket"]
    assert dev.branch == "dev"
    assert dev.owner == "registry-owner"
    assert "CI Summary" in dev.required_checks
    assert "repo-evidence / dod-verify" in dev.required_checks
    assert "OmniNode-ai/overlays" not in target_by_repo

    with pytest.raises(ValueError, match="stale"):
        read_inventory(
            watcher, last_tick + timedelta(hours=1), scm_owner="", registry=registry
        )


async def test_red_main_alarm_writer_accepts_the_pair_and_rejects_changed_replay(
    tmp_path: Path,
) -> None:
    class LedgerWriter(LedgerAndScm):
        def __init__(self) -> None:
            super().__init__()
            ledger = tmp_path / "ledger.md"
            ledger.touch()
            self.store = InMemoryBusLedgerStore(head=0)
            self.writer = HandlerWorkLedgerAppendEffect(
                reader=LocalLedgerFile(ledger),
                writer_principals=("test-writer",),
                host_name="lab",
                fence=LedgerFlipFence(EnumLedgerAppendMode.BUS),
                bus_store=self.store,
            )

        async def append(
            self, request: ModelWorkLedgerAppendRequest
        ) -> ModelWorkLedgerAppendReceipt:
            self.requests[request.request_id] = request
            return self.writer.handle(request, writer_principal="test-writer")

    ports = LedgerWriter()
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)
    first = await handler.handle(tick())

    assert not first.failures
    assert len(first.alarms) == 1
    (request,) = ports.requests.values()
    [(_, status), (_, message)] = ports.store.rows_by_seq()
    assert f"owner={PARENT} | owner_source=none" in status
    assert f"to={PARENT} | " in message

    duplicate = ports.writer.handle(request, writer_principal="test-writer")
    assert duplicate.status == EnumWorkLedgerAppendStatus.DUPLICATE
    changed = request.model_copy(
        update={"rows": request.rows.replace(PARENT, "new-owner")}
    )
    refused = ports.writer.handle(changed, writer_principal="test-writer")
    assert refused.status == EnumWorkLedgerAppendStatus.REFUSED
    assert len(list(ports.store.rows_by_seq())) == 2


async def test_red_main_alarm_retry_keeps_the_first_red_read() -> None:
    class TemporarilyUnreadableLedger(LedgerAndScm):
        available = False

        async def open_claims(
            self, now: datetime
        ) -> tuple[ModelWorkLedgerOpenClaim, ...]:
            if not self.available:
                raise OSError("claim source unavailable")
            return ()

    ports = TemporarilyUnreadableLedger()
    now = NOW
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: now)

    failed = await handler.handle(tick(now))
    assert failed.failures[0].kind == EnumAlarmFailure.CLAIMS_UNREADABLE
    assert not ports.requests

    ports.available = True
    now += timedelta(minutes=10)
    retried = await handler.handle(tick(now))
    assert not retried.failures
    assert retried.alarms[0].first_red_at == NOW


async def test_red_main_alarm_bus_carries_the_runtime_tick_to_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_root = Path(__file__).parents[3] / "src/omnimarket/nodes"
    alarm_contract = node_root / "node_red_main_alarm_effect/contract.yaml"
    manifest = discover_contracts_from_paths([alarm_contract])
    assert not manifest.errors
    alarm = yaml.safe_load(alarm_contract.read_text())
    alarm_topic = alarm["terminal_event"]
    # The result event the subscriber below reads is the contract's declared
    # output, published and externally consumed under that one name.
    assert alarm_topic == "onex.evt.omnimarket.red-main-alarm-completed.v1"
    assert alarm_topic in alarm["event_bus"]["publish_topics"]
    assert alarm_topic in alarm["externally_consumed_topics"]
    tick_topic = alarm["input_subscriptions"][0]["topic"]
    ports = LedgerAndScm()
    handler = HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: NOW)
    monkeypatch.setattr(
        "omnimarket.nodes.node_red_main_alarm_effect.handler.HandlerRedMainAlarm",
        lambda: handler,
    )
    bus = EventBusInmemory(environment="red-main-alarm", group="red-main-alarm")
    results: asyncio.Queue[ModelRedMainAlarmResult] = asyncio.Queue()

    async def read_result(message: ModelEventMessage) -> None:
        body = json.loads(message.value)
        results.put_nowait(ModelRedMainAlarmResult.model_validate(body["payload"]))

    await bus.start()
    try:
        await bus.subscribe(alarm_topic, on_message=read_result, group_id="alarm-test")
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0
        assert report.total_wired == 1
        engine.freeze()

        # A tick inside a window the scan already took opens nothing; the tick that
        # opens the 15-minute window carries the scan.
        for index, at in enumerate((WINDOW + timedelta(minutes=5), WINDOW), start=1):
            platform_tick = ModelRuntimeTick(
                now=at,
                tick_id=uuid4(),
                sequence_number=index,
                scheduled_at=at,
                correlation_id=uuid4(),
                scheduler_id="test",
                tick_interval_ms=1000,
            )
            envelope = ModelEventEnvelope[ModelRuntimeTick](
                payload=platform_tick,
                event_type="platform.runtime-tick",
                correlation_id=platform_tick.correlation_id,
            )
            await bus.publish(
                tick_topic, None, envelope.model_dump_json().encode(), None
            )
        result = await asyncio.wait_for(results.get(), timeout=8)
    finally:
        await bus.close()

    assert results.empty()
    assert result.fire_id == "red-main-alarm-20261119T034500Z"
    assert not result.failures
    assert len(result.alarms) == 1
    assert len(ports.requests) == 1


def runtime_tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


async def test_red_main_alarm_raises_one_alarm_per_red_head_over_two_ticks() -> None:
    """Two consecutive 15-minute windows over one red head write one pair; a new red head writes one more."""
    ports = LedgerAndScm()
    scheduled = HandlerRedMainAlarmScheduled(
        HandlerRedMainAlarm(ports=ports, settings=SETTINGS, clock=lambda: WINDOW)
    )

    first = await scheduled.handle(runtime_tick(WINDOW))
    between = await scheduled.handle(runtime_tick(WINDOW + timedelta(minutes=5)))
    second = await scheduled.handle(runtime_tick(WINDOW + timedelta(minutes=15)))

    assert first is not None
    assert len(first.alarms) == 1
    assert between is None
    assert second is not None
    assert second.fire_id == "red-main-alarm-20261119T040000Z"
    assert not second.alarms
    assert not second.failures
    assert len(ports.requests) == 1

    ports.move_to("other_red")
    third = await scheduled.handle(runtime_tick(WINDOW + timedelta(minutes=30)))
    assert third is not None
    assert len(third.alarms) == 1
    assert len(ports.requests) == 2


class RecordedGateway:
    """The SCM gateway as the overlays main read recorded it: one workflow-run answer."""

    def __init__(self, recorded_answer: dict[str, Any]) -> None:
        self.recorded_answer = recorded_answer
        self.requests: list[ModelGithubGatewayRequest] = []

    async def call(
        self,
        *,
        node: object,
        request: ModelGithubGatewayRequest,
        correlation_id: UUID,
        timeout_s: float,
    ) -> object:
        self.requests.append(request)
        assert request.operation == EnumGithubGatewayOperation.WORKFLOW_RUNS
        payload = {"status": "completed", "result": self.recorded_answer}
        return type("Terminal", (), {"failed": False, "payload": payload})()


def overlays_main_fixture() -> dict[str, Any]:
    recorded_fixture: dict[str, Any] = json.loads(
        (FIXTURES / "overlays_main_red.json").read_text()
    )
    return recorded_fixture


MAP = ModelAlarmSettings(
    parent_lane=PARENT,
    host="h201",
    repositories=(
        ModelAlarmRepository(
            repo="OmniNode-ai/overlays",
            branch="main",
            owner=PARENT,
            required_checks=("ci",),
        ),
    ),
)


async def test_red_main_alarm_reads_overlays_main_with_no_watcher_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The earlier silence, recorded: the runtime held no PR watcher snapshot and the
    snapshot's settings read of overlays main named no required context, so the scan
    skipped it. With the repository map the head and the context are declared."""
    recorded_fixture = overlays_main_fixture()
    read_at = datetime(2026, 11, 20, 0, 30, tzinfo=UTC)
    registry = ModelRepositoryRegistryOverlay.model_validate(
        {
            "repositories": [
                {
                    "name": "overlays",
                    "repo": "OmniNode-ai/overlays",
                    "type": "other",
                    "description": "deployment overlays",
                }
            ]
        }
    )
    watcher = tmp_path / "state.json"
    watcher.write_text(
        json.dumps(
            {
                "schema": WATCHER_SCHEMA,
                "last_tick": read_at.isoformat(),
                "repos": {"overlays": {"default_branch": "main"}},
                "branches": {
                    "overlays@main": {
                        "sha": "24186de77a07413b4ab75aaa55fd103996afd2fa",
                        "tip_read_at": read_at.isoformat(),
                    }
                },
                "settings": recorded_fixture["watcher_settings"],
            }
        )
    )
    skipped = read_inventory(
        watcher, read_at, scm_owner="OmniNode-ai", registry=registry
    )
    assert not skipped.failures
    assert not skipped.targets

    gateway = RecordedGateway(recorded_fixture["workflow_runs"])
    ports = AlarmBusPorts(
        None,
        MAP,
        EventBusInmemory(environment="test", group="test"),
        cast(Any, object()),
    )
    monkeypatch.setattr(ports, "_calls", gateway)
    monkeypatch.setattr(
        ports_module, "load_structured_overlay", lambda *_a, **_k: registry
    )
    inventory = await ports.inventory(NOW)

    assert not inventory.failures
    (target,) = inventory.targets
    assert target.repo == "OmniNode-ai/overlays"
    assert target.head_sha == "24186de77a07413b4ab75aaa55fd103996afd2fa"
    assert target.required_checks == ("ci",)
    assert target.owner is None
    assert target.declared_owner == PARENT
    (request,) = gateway.requests
    assert (request.branch, request.event) == ("main", "push")

    scm = LedgerAndScm()
    scm.answer = ModelCommitCheckRunsResult.model_validate(
        recorded_fixture["check_runs"]
    )
    scm.target = target
    result = await HandlerRedMainAlarm(
        ports=scm, settings=MAP, clock=lambda: NOW
    ).handle(tick())

    assert not result.failures
    (alarm,) = result.alarms
    assert (alarm.owner, alarm.owner_source) == (PARENT, "declared")
    (row,) = scm.requests.values()
    status = row.rows.splitlines()[0]
    assert re.search(r"src=alarm.*alarm=main-red.*OmniNode-ai/overlays", status)
    assert f" | owner={PARENT} | owner_source=declared | " in status
    assert " | drill=yes" not in status


async def test_red_main_alarm_gateway_head_failure_is_typed_per_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RecordedGateway(
        {
            "operation": "workflow_runs",
            "repo": "OmniNode-ai/overlays",
            "total_count": 0,
            "truncated": False,
            "runs": [],
        }
    )
    ports = AlarmBusPorts(
        None,
        MAP,
        EventBusInmemory(environment="test", group="test"),
        cast(Any, object()),
    )
    monkeypatch.setattr(ports, "_calls", gateway)
    registry = ModelRepositoryRegistryOverlay.model_validate(
        {
            "repositories": [
                {
                    "name": "x",
                    "repo": "OmniNode-ai/x",
                    "type": "other",
                    "description": "x",
                }
            ]
        }
    )
    monkeypatch.setattr(
        ports_module, "load_structured_overlay", lambda *_a, **_k: registry
    )

    inventory = await ports.inventory(NOW)

    assert not inventory.targets
    (failure,) = inventory.failures
    assert failure.kind == EnumAlarmFailure.INVENTORY_UNREADABLE
    assert failure.repo == "OmniNode-ai/overlays"
