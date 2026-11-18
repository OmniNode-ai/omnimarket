# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R8, a synthetic delegation run is labelled and listed but never counted in a tenant's totals (OMN-23175, plan 2.2).

In a probe tenant only this scenario uses, one run:

1. reads the tenant's totals (runs, tokens, spend, savings);
2. sends one synthetic run (``--traffic-class synthetic --source-surface readiness_probe``) and one organic control;
3. waits until the tenant's Runs list holds both correlation ids;
4. reads the totals again.

It holds only when the synthetic row is labelled synthetic and listed on Runs, the organic row is labelled organic and counted
(its own values are not all zero, so a pass cannot be vacuous), and the totals rose by exactly the organic run's values: one run,
its tokens, its spend and its savings. A synthetic run that is counted raises the run total by two and fails. Every assertion
compares typed fields; no model judges a pass. A lane that cannot be read, a wait that times out and an exception are failures,
never a skip. The result goes to the ledger first (one STATUS row, the row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import os
import shlex
import socket
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.delegation_readiness_probe import ScriptedLegTransport
from omnimarket.handlers.delegation_readiness_scenario import (
    ModelReadinessRuntime,
    ReadinessScenarioBase,
    load_terminal_topic,
    read_contract,
)
from omnimarket.handlers.delegation_readiness_scenario import (
    load_schedule as load_schedule_from,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
)
from omnimarket.handlers.delegation_scenario_settings import (
    ProtocolSettingStore,
    SecretStoreSettingsReader,
    fill_overlay_settings,
    load_store_overlay,
    load_tenant_read_overlay,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_bus_request import (
    build_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    BusLane,
    HttpLane,
    InMemoryLane,
    LaneReadError,
    ModelLaneSettings,
    ModelRunRow,
    ModelSendRequest,
    ModelTotals,
    ProtocolR8Lane,
    delegate_transport_args,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r8_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"

Clock = Callable[[], datetime]
LaneFactory = Callable[[], ProtocolR8Lane]


class ModelScenarioR8RunCommand(BaseModel):
    """One run of scenario R8. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelTrafficKind(BaseModel):
    """How one of the two runs is asked for: its traffic class and, for a synthetic run, its source surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    traffic_class: str = Field(min_length=1)
    source_surface: str | None = None


class ModelR8Inputs(BaseModel):
    """The committed inputs file of scenario R8."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    wait_timeout_seconds: float = Field(gt=0)
    poll_interval_seconds: float = Field(gt=0)
    money_decimal_places: int = Field(ge=0, le=12)
    synthetic: ModelTrafficKind
    organic: ModelTrafficKind

    def request(self, kind: ModelTrafficKind) -> ModelSendRequest:
        return ModelSendRequest(
            task_type=self.task_type,
            prompt=self.prompt,
            traffic_class=kind.traffic_class,
            source_surface=kind.source_surface,
        )

    def organic_request(self) -> ModelSendRequest:
        """The control is asked for with no traffic class at all: an organic run is the default."""
        return ModelSendRequest(task_type=self.task_type, prompt=self.prompt)

    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.money_decimal_places)


def load_inputs(path: Path) -> ModelR8Inputs:
    return ModelR8Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessRuntime:
    return ModelReadinessRuntime.model_validate(read_contract(path)["runtime"])


def load_lane_settings(path: Path = CONTRACT_PATH) -> ModelLaneSettings:
    return ModelLaneSettings.model_validate(read_contract(path)["lane"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


class HandlerScenarioR8(ReadinessScenarioBase):
    """Run scenario R8 once, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelReadinessRuntime,
        *,
        home: Path,
        environment: Mapping[str, str],
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str,
        lane_factory: LaneFactory,
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
        inputs_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            runtime,
            home=home,
            environment=environment,
            appender=appender,
            sink=sink,
            terminal_topic=terminal_topic,
            # R8 runs no probe ladder: the base's leg transport is never built.
            transport_factory=lambda: ScriptedLegTransport(),
            trigger=trigger,
            clock=clock,
        )
        self._inputs_path = inputs_path or NODE_DIR / runtime.inputs_relpath
        self._lane_factory = lane_factory
        self._sleep = sleep
        self._monotonic = monotonic

    def handle(self, request: ModelScenarioR8RunCommand) -> ModelScenarioResult:
        return self.run()

    # -- the scenario ---------------------------------------------------------------------------------------------

    def _wait_for_rows(
        self, lane: ProtocolR8Lane, wanted: tuple[str, ...], inputs: ModelR8Inputs
    ) -> dict[str, ModelRunRow] | None:
        """The rows for ``wanted`` once the Runs list holds all of them, or None when the wait ran out."""
        deadline = self._monotonic() + inputs.wait_timeout_seconds
        while True:
            held = {row.correlation_id: row for row in lane.read_runs()}
            if all(item in held for item in wanted):
                return {item: held[item] for item in wanted}
            if self._monotonic() >= deadline:
                return None
            self._sleep(inputs.poll_interval_seconds)

    def _scenario(self, assertions: list[str]) -> str:
        problems: list[str] = []

        def check(name: str, held: bool, why: str = "") -> bool:
            assertions.append(("+" if held else "-") + name)
            if not held and why:
                problems.append(f"{name}: {why}")
            return held

        try:
            inputs = load_inputs(self._inputs_path)
        except (OSError, ValueError) as exc:
            assertions.append("-inputs_readable")
            return f"inputs unreadable: {type(exc).__name__}"
        assertions.append("+inputs_readable")

        try:
            lane = self._lane_factory()
        except LaneReadError as exc:
            assertions.append("-lane_configured")
            return f"lane not configured: {exc}"
        assertions.append("+lane_configured")

        try:
            before = lane.read_totals()
            check("totals_read_before", True)
        except LaneReadError as exc:
            assertions.append("-totals_read_before")
            return f"totals before: {exc}"

        try:
            synthetic_id = lane.send(inputs.request(inputs.synthetic))
            organic_id = lane.send(inputs.organic_request())
        except LaneReadError as exc:
            assertions.append("-runs_sent")
            return f"send: {exc}"
        if not check(
            "runs_sent_distinct",
            synthetic_id != organic_id,
            f"both runs carry the correlation id {synthetic_id}",
        ):
            return "; ".join(problems)

        try:
            rows = self._wait_for_rows(lane, (synthetic_id, organic_id), inputs)
        except LaneReadError as exc:
            assertions.append("-runs_read")
            return f"runs: {exc}"
        if not check(
            "projection_holds_both",
            rows is not None,
            f"the Runs list did not hold both ids within {inputs.wait_timeout_seconds:g} s",
        ):
            return "; ".join(problems)
        assert rows is not None
        synthetic_row, organic_row = rows[synthetic_id], rows[organic_id]

        try:
            after = lane.read_totals()
            check("totals_read_after", True)
        except LaneReadError as exc:
            assertions.append("-totals_read_after")
            return f"totals after: {exc}"

        check(
            "synthetic_listed_labelled_synthetic",
            synthetic_row.traffic_class == inputs.synthetic.traffic_class,
            f"the synthetic row reads {synthetic_row.traffic_class}",
        )
        check(
            "organic_listed_labelled_organic",
            organic_row.traffic_class == inputs.organic.traffic_class,
            f"the organic row reads {organic_row.traffic_class}",
        )
        check(
            "organic_run_has_values",
            organic_row.tokens > 0 and organic_row.cost_usd > 0,
            "the organic control carries no tokens or no spend, so equal deltas would prove nothing",
        )
        self._check_totals(before, after, organic_row, inputs, check)
        return "; ".join(problems) or "every assertion held"

    @staticmethod
    def _check_totals(
        before: ModelTotals,
        after: ModelTotals,
        organic: ModelRunRow,
        inputs: ModelR8Inputs,
        check: Callable[[str, bool, str], bool],
    ) -> None:
        quantum = inputs.quantum()
        spend = (after.spend_usd - before.spend_usd).quantize(quantum)
        savings = (after.savings_usd - before.savings_usd).quantize(quantum)
        check(
            "run_total_rose_by_one",
            after.runs - before.runs == 1,
            f"runs rose by {after.runs - before.runs}, the organic control alone is 1",
        )
        check(
            "token_total_rose_by_organic_tokens",
            after.tokens - before.tokens == organic.tokens,
            f"tokens rose by {after.tokens - before.tokens}, the organic run has {organic.tokens}",
        )
        check(
            "spend_total_rose_by_organic_cost",
            spend == organic.cost_usd.quantize(quantum),
            f"spend rose by {spend}, the organic run cost {organic.cost_usd}",
        )
        check(
            "savings_total_rose_by_organic_saved",
            savings == organic.saved_usd.quantize(quantum),
            f"savings rose by {savings}, the organic run saved {organic.saved_usd}",
        )


def _record_appender(record: Path) -> LedgerAppender:
    """A hosted runner holds no ledger route: the STATUS row goes to the record file the workflow uploads."""

    def append(row: str) -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        with record.open("a", encoding="utf-8") as handle:
            handle.write(row.rstrip("\n") + "\n")

    return append


def _build_inmemory_lane(
    settings: ModelLaneSettings,
    environment: Mapping[str, str],
    tenant_read: ModelScenarioTenantReadOverlay | None,
) -> ProtocolR8Lane:
    """The typed in-memory model of the tenant lane; it reads no setting and no environment."""
    del settings, environment, tenant_read
    return InMemoryLane()


def _build_http_lane(
    settings: ModelLaneSettings,
    environment: Mapping[str, str],
    tenant_read: ModelScenarioTenantReadOverlay | None,
) -> ProtocolR8Lane:
    """The real tenant API, addressed and keyed from the environment variables the contract names."""
    del tenant_read
    return HttpLane(settings, environment)


def _build_bus_lane(
    settings: ModelLaneSettings,
    environment: Mapping[str, str],
    tenant_read: ModelScenarioTenantReadOverlay | None,
) -> ProtocolR8Lane:
    """The tenant reads over the event bus; the bus, lane and probe tenant come from the R8 overlay document."""
    if tenant_read is None:
        raise LaneReadError("the R8 overlay document has no tenant_read block")
    if settings.bus is None:
        raise LaneReadError("the R8 contract lane block has no bus block")
    workspace = environment.get(settings.bus.workspace_env, "").strip() or None
    delegate_transport_args(tenant_read, workspace)
    return BusLane(
        settings,
        tenant_read,
        build_bus_request(
            tenant_read,
            savings_node=settings.bus.savings_node,
            delegations_node=settings.bus.delegations_node,
            workspace_env=settings.bus.workspace_env,
            environment=environment,
        ),
        workspace=workspace,
    )


#: The contract's ``lane.backend`` value picks the lane from this table; the pattern on ``ModelLaneSettings.backend`` admits
#: exactly these keys, so a backend the table lacks cannot reach it.
LANE_BUILDERS: Mapping[
    str,
    Callable[
        [ModelLaneSettings, Mapping[str, str], ModelScenarioTenantReadOverlay | None],
        ProtocolR8Lane,
    ],
] = {
    "inmemory": _build_inmemory_lane,
    "http": _build_http_lane,
    "bus": _build_bus_lane,
}


def r8_settings_store(
    environ: Mapping[str, str] | None = None,
) -> SecretStoreSettingsReader:
    """The R8 key store reader: its store block is the R8 overlay document's when the deployment supplies one."""
    return SecretStoreSettingsReader(overlay=load_store_overlay(NODE_NAME, environ))


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
    record: Path | None = None,
    settings_store: ProtocolSettingStore | None = None,
    settings_home: Path | None = None,
) -> HandlerScenarioR8:
    """The production wiring: the registry home, the bus commands and the contract's lane backend."""
    runtime = load_runtime()
    settings = load_lane_settings()
    environment = fill_overlay_settings(
        dict(os.environ if env is None else env),  # ONEX_FLAG_EXEMPT
        runtime.settings_overlay_relpath,
        node_dir=CONTRACT_PATH.parent,
        store=settings_store,
        home=settings_home,
    )
    home = Path(environment[runtime.home_env])
    if record is not None:
        appender = _record_appender(record)
    else:
        append_argv = shlex.split(
            environment.get(runtime.ledger_append_command_env, "")
        )
        if not append_argv:
            raise RuntimeError(
                f"{runtime.ledger_append_command_env} is not set; the result has no ledger route"
            )
        host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
        appender = bus_appender(
            CommandLedgerAppender(
                append_argv, runtime.lane, host, runtime.ledger_append_timeout_seconds
            )
        )

    def lane_factory() -> ProtocolR8Lane:
        return LANE_BUILDERS[settings.backend](
            settings, environment, load_tenant_read_overlay(NODE_NAME, environment)
        )

    return HandlerScenarioR8(
        runtime,
        home=home,
        environment=environment,
        appender=appender,
        sink=build_event_sink(
            environment,
            command_env=runtime.bus_emit_command_env,
            journal=home / runtime.event_journal_relpath,
            requested_by=runtime.lane,
            timeout_seconds=runtime.bus_emit_timeout_seconds,
        ),
        terminal_topic=load_terminal_topic(CONTRACT_PATH),
        lane_factory=lane_factory,
        trigger=trigger,
    )
