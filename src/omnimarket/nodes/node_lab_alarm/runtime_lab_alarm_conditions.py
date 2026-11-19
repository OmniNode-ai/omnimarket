# SPDX-License-Identifier: Apache-2.0
"""Runtime wiring for the ONEX node_lab_alarm condition verbs.

This module wires the four new lab-alarm operations (lane_census,
lab_pass_receipt, loaded_code_sha, stale_indeterminate) to the CLI, following the shape of
the dev-tag-heartbeat exemplar: handler builders are chosen from tables
keyed by the overlay's bus backend, and each run_* function prints
EVENT / summary / ALARM / DELIVERY lines and returns an exit code.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_lab_alarm.adapters.condition_adapters import (
    CommandCensusSource,
    FileCensusSource,
    FileRunRecordReader,
    InMemoryCensusSource,
    InMemoryDeliveredShaSource,
    InMemoryLabPassReceiptSource,
    InMemoryRunRecordReader,
)
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRunRecords,
)
from omnimarket.nodes.node_lab_alarm.adapters.receipt_adapters import (
    GatewayLabPassReceiptSource,
    HttpDeliveredShaSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.restart_adapters import (
    InMemoryRestartBounds,
    InMemoryRestartReader,
    SshDockerRestartReader,
    YamlLaneManifestBounds,
)
from omnimarket.nodes.node_lab_alarm.adapters.routed_delivery_adapters import (
    build_routed_delivery,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import FileRunRecords
from omnimarket.nodes.node_lab_alarm.handlers.alarm_route import (
    HandlerAlarmRoute,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_container_restarts import (
    HandlerContainerRestarts,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_container_restarts import (
    events_for as container_restarts_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_container_restarts import (
    load_topics as container_restarts_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    HandlerLabPassReceipt,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    events_for as lab_pass_receipt_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lab_pass_receipt import (
    load_topics as lab_pass_receipt_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    HandlerLaneCensus,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    events_for as lane_census_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_lane_census import (
    load_topics as lane_census_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_loaded_code_sha import (
    HandlerLoadedCodeSha,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_loaded_code_sha import (
    events_for as loaded_code_sha_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_loaded_code_sha import (
    load_topics as loaded_code_sha_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_stale_indeterminate import (
    HandlerStaleIndeterminate,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_stale_indeterminate import (
    events_for as stale_indeterminate_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_stale_indeterminate import (
    load_topics as stale_indeterminate_load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_container_restarts import (
    EnumContainerRestartsFault,
    EnumContainerRestartsOutcome,
    ModelContainerRestartsCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lab_pass_receipt import (
    EnumLabPassReceiptFault,
    EnumLabPassReceiptOutcome,
    ModelLabPassReceiptCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_lane_census import (
    EnumLaneCensusCondition,
    EnumLaneCensusFault,
    EnumLaneCensusOutcome,
    ModelLaneCensusCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_loaded_code_sha import (
    EnumLoadedCodeShaOutcome,
    ModelLoadedCodeShaRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_stale_indeterminate import (
    EnumStaleIndeterminateFault,
    EnumStaleIndeterminateOutcome,
    ModelStaleIndeterminateCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelContainerRestartsSettings,
    ModelLabAlarmOverlay,
    ModelLabPassReceiptSettings,
    ModelLaneCensusSettings,
    ModelStaleIndeterminateSettings,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolContainerRestartReader,
    ProtocolRestartBoundSource,
    RunRecordUnwritableError,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    load_overlay as load_delivery_overlay,
)

__all__ = [
    "CONDITION_VERBS",
    "JOURNALED_EXIT_VERBS",
    "add_condition_parsers",
    "build_container_restarts_handler",
    "build_lab_pass_receipt_handler",
    "build_lane_census_handler",
    "build_loaded_code_sha_handler",
    "build_stale_indeterminate_handler",
    "journal_exit",
    "run_condition_verb",
    "run_container_restarts",
    "run_lab_pass_receipt",
    "run_lane_census",
    "run_loaded_code_sha",
    "run_stale_indeterminate",
]

# ---------------------------------------------------------------------------
# Delivery / records helpers
# ---------------------------------------------------------------------------


def _delivery_for(overlay: ModelLabAlarmOverlay) -> HandlerAlarmRoute | None:
    """Return the delivery handler when the overlay declares and enables it."""
    if overlay.alarm_delivery is None or not overlay.alarm_delivery.enabled:
        return None
    return build_routed_delivery(load_delivery_overlay(), alarm_overlay=overlay)


def _inmemory_records(
    overlay: ModelLabAlarmOverlay,
) -> InMemoryRunRecords | FileRunRecords | None:
    return InMemoryRunRecords()


def _runtime_records(
    overlay: ModelLabAlarmOverlay,
) -> InMemoryRunRecords | FileRunRecords | None:
    if overlay.verdict_record is None:
        return None
    return FileRunRecords(Path(overlay.verdict_record.path))


_RECORDS_BACKEND_BUILDERS: dict[
    str,
    Callable[[ModelLabAlarmOverlay], InMemoryRunRecords | FileRunRecords | None],
] = {
    "inmemory": _inmemory_records,
    "runtime": _runtime_records,
}


def _records_for(
    overlay: ModelLabAlarmOverlay,
) -> InMemoryRunRecords | FileRunRecords | None:
    """The run-record store of the overlay's backend; the runtime backend keeps none until verdict_record is declared."""
    return _RECORDS_BACKEND_BUILDERS[overlay.bus.backend](overlay)


# ---------------------------------------------------------------------------
# Handler builders
# ---------------------------------------------------------------------------


def _inmemory_lane_census_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLaneCensusSettings,
    condition: EnumLaneCensusCondition,
    clock: Callable[[], datetime],
) -> HandlerLaneCensus:
    return HandlerLaneCensus(
        condition=condition,
        settings=settings,
        source=InMemoryCensusSource(error="no fixture census"),
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


def _runtime_lane_census_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLaneCensusSettings,
    condition: EnumLaneCensusCondition,
    clock: Callable[[], datetime],
) -> HandlerLaneCensus:
    if settings.snapshot_command is not None:
        source: CommandCensusSource | FileCensusSource = CommandCensusSource(
            settings.snapshot_command,
            settings.timeout_seconds,
        )
    else:
        source = FileCensusSource(Path(settings.snapshot_path))
    return HandlerLaneCensus(
        condition=condition,
        settings=settings,
        source=source,
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


_LANE_CENSUS_BACKEND_BUILDERS: dict[
    str,
    Callable[
        [
            ModelLabAlarmOverlay,
            ModelLaneCensusSettings,
            EnumLaneCensusCondition,
            Callable[[], datetime],
        ],
        HandlerLaneCensus,
    ],
] = {
    "inmemory": _inmemory_lane_census_handler,
    "runtime": _runtime_lane_census_handler,
}


def build_lane_census_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLaneCensusSettings,
    condition: EnumLaneCensusCondition,
    clock: Callable[[], datetime],
) -> HandlerLaneCensus:
    """The lane-census handler wired to the overlay's backend."""
    return _LANE_CENSUS_BACKEND_BUILDERS[overlay.bus.backend](
        overlay, settings, condition, clock
    )


def _inmemory_lab_pass_receipt_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLabPassReceipt:
    return HandlerLabPassReceipt(
        settings=settings,
        sha_source=InMemoryDeliveredShaSource(error="no fixture sha"),
        receipts=InMemoryLabPassReceiptSource(),
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


def _runtime_lab_pass_receipt_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLabPassReceipt:
    return HandlerLabPassReceipt(
        settings=settings,
        sha_source=HttpDeliveredShaSource(
            settings.agent_url,
            settings.ready_url,
            settings.timeout_seconds,
        ),
        receipts=GatewayLabPassReceiptSource(settings),
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


_LAB_PASS_RECEIPT_BACKEND_BUILDERS: dict[
    str,
    Callable[
        [ModelLabAlarmOverlay, ModelLabPassReceiptSettings, Callable[[], datetime]],
        HandlerLabPassReceipt,
    ],
] = {
    "inmemory": _inmemory_lab_pass_receipt_handler,
    "runtime": _runtime_lab_pass_receipt_handler,
}


def build_lab_pass_receipt_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLabPassReceipt:
    """The lab-pass-receipt handler wired to the overlay's backend."""
    return _LAB_PASS_RECEIPT_BACKEND_BUILDERS[overlay.bus.backend](
        overlay, settings, clock
    )


def _inmemory_loaded_code_sha_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLoadedCodeSha:
    return HandlerLoadedCodeSha(
        sha_source=InMemoryDeliveredShaSource(error="no fixture sha")
    )


def _runtime_loaded_code_sha_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLoadedCodeSha:
    return HandlerLoadedCodeSha(
        sha_source=HttpDeliveredShaSource(
            settings.agent_url,
            settings.ready_url,
            settings.timeout_seconds,
        )
    )


_LOADED_CODE_SHA_BACKEND_BUILDERS: dict[
    str,
    Callable[
        [ModelLabAlarmOverlay, ModelLabPassReceiptSettings, Callable[[], datetime]],
        HandlerLoadedCodeSha,
    ],
] = {
    "inmemory": _inmemory_loaded_code_sha_handler,
    "runtime": _runtime_loaded_code_sha_handler,
}


def build_loaded_code_sha_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelLabPassReceiptSettings,
    clock: Callable[[], datetime],
) -> HandlerLoadedCodeSha:
    """The loaded-code-sha handler wired to the overlay's backend."""
    return _LOADED_CODE_SHA_BACKEND_BUILDERS[overlay.bus.backend](
        overlay, settings, clock
    )


def _inmemory_stale_indeterminate_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelStaleIndeterminateSettings,
    clock: Callable[[], datetime],
) -> HandlerStaleIndeterminate:
    return HandlerStaleIndeterminate(
        settings=settings,
        reader=InMemoryRunRecordReader(),
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


def _runtime_stale_indeterminate_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelStaleIndeterminateSettings,
    clock: Callable[[], datetime],
) -> HandlerStaleIndeterminate:
    watched_paths: Mapping[str, Path] = {
        w.condition: Path(w.record_path) for w in settings.watched
    }
    return HandlerStaleIndeterminate(
        settings=settings,
        reader=FileRunRecordReader(watched_paths, settings.tail_bytes),
        delivery=_delivery_for(overlay),
        records=_records_for(overlay),
        clock=clock,
    )


_STALE_INDETERMINATE_BACKEND_BUILDERS: dict[
    str,
    Callable[
        [ModelLabAlarmOverlay, ModelStaleIndeterminateSettings, Callable[[], datetime]],
        HandlerStaleIndeterminate,
    ],
] = {
    "inmemory": _inmemory_stale_indeterminate_handler,
    "runtime": _runtime_stale_indeterminate_handler,
}


def build_stale_indeterminate_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelStaleIndeterminateSettings,
    clock: Callable[[], datetime],
) -> HandlerStaleIndeterminate:
    """The stale-indeterminate handler wired to the overlay's backend."""
    return _STALE_INDETERMINATE_BACKEND_BUILDERS[overlay.bus.backend](
        overlay, settings, clock
    )


def _inmemory_restart_sources(
    settings: ModelContainerRestartsSettings,
) -> tuple[ProtocolRestartBoundSource, ProtocolContainerRestartReader]:
    return InMemoryRestartBounds(), InMemoryRestartReader()


def _runtime_restart_sources(
    settings: ModelContainerRestartsSettings,
) -> tuple[ProtocolRestartBoundSource, ProtocolContainerRestartReader]:
    return (
        YamlLaneManifestBounds(
            settings.lane_manifest, settings.census_lane, os.environ
        ),
        SshDockerRestartReader(settings.docker_command),
    )


_RESTART_SOURCES: dict[
    str,
    Callable[
        [ModelContainerRestartsSettings],
        tuple[ProtocolRestartBoundSource, ProtocolContainerRestartReader],
    ],
] = {
    "inmemory": _inmemory_restart_sources,
    "runtime": _runtime_restart_sources,
}


def build_container_restarts_handler(
    overlay: ModelLabAlarmOverlay, settings: ModelContainerRestartsSettings
) -> HandlerContainerRestarts:
    """The restarts handler wired to the overlay's backend: the lane manifest and ssh docker on the runtime backend, fixtures in memory."""
    bounds, reader = _RESTART_SOURCES[overlay.bus.backend](settings)
    return HandlerContainerRestarts(
        settings=settings,
        bounds=bounds,
        reader=reader,
        delivery=_delivery_for(overlay),
    )


# ---------------------------------------------------------------------------
# Exit-code tables
# ---------------------------------------------------------------------------

_LANE_CENSUS_EXIT: dict[EnumLaneCensusOutcome, int] = {
    EnumLaneCensusOutcome.OK: 0,
    EnumLaneCensusOutcome.ALARM: 2,
    EnumLaneCensusOutcome.INDETERMINATE: 3,
}

_CONTAINER_RESTARTS_EXIT: dict[EnumContainerRestartsOutcome, int] = {
    EnumContainerRestartsOutcome.OK: 0,
    EnumContainerRestartsOutcome.ALARM: 2,
    EnumContainerRestartsOutcome.INDETERMINATE: 3,
}

_LAB_PASS_RECEIPT_EXIT: dict[EnumLabPassReceiptOutcome, int] = {
    EnumLabPassReceiptOutcome.OK: 0,
    EnumLabPassReceiptOutcome.ALARM: 2,
    EnumLabPassReceiptOutcome.INDETERMINATE: 3,
}

_LOADED_CODE_SHA_EXIT: dict[EnumLoadedCodeShaOutcome, int] = {
    EnumLoadedCodeShaOutcome.READ: 0,
    EnumLoadedCodeShaOutcome.UNREAD: 3,
}

_STALE_INDETERMINATE_EXIT: dict[EnumStaleIndeterminateOutcome, int] = {
    EnumStaleIndeterminateOutcome.OK: 0,
    EnumStaleIndeterminateOutcome.ALARM: 2,
    EnumStaleIndeterminateOutcome.INDETERMINATE: 3,
}


# ---------------------------------------------------------------------------
# run_* functions
# ---------------------------------------------------------------------------


def run_lane_census(
    verb: str,
    fault: EnumLaneCensusFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerLaneCensus | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One lane-census tick."""
    settings = overlay.lane_census
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED lane_census: the overlay has no lane_census block; "
            "the other verbs do not need it\n"
        )
        return 1
    condition = EnumLaneCensusCondition(verb.replace("-probe", "").replace("-", "_"))
    write = not verb.endswith("-probe")
    if handler is None:
        handler = build_lane_census_handler(
            overlay, settings, condition, lambda: datetime.now(UTC)
        )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelLaneCensusCommand(
            correlation_id=f"lab-alarm-{verb.replace('-probe', '')}-{now}",
            write=write,
            fault=fault,
        )
    )
    for topic, body in lane_census_events_for(result, lane_census_load_topics()):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"LANE-CENSUS condition={condition.value} outcome={result.outcome.value} "
        f"fault={result.fault.value} host={result.host} "
        f"alarms={len(result.alarms)} reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if result.failure.startswith("record-unwritable"):
        return 4
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return 2
    return _LANE_CENSUS_EXIT[result.outcome]


def run_container_restarts(
    verb: str,
    fault: EnumContainerRestartsFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerContainerRestarts | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One container-restarts tick."""
    settings = overlay.container_restarts
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED container_restarts: the overlay has no container_restarts block; "
            "the other verbs do not need it\n"
        )
        return 1
    if handler is None:
        handler = build_container_restarts_handler(overlay, settings)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelContainerRestartsCommand(
            correlation_id=f"lab-alarm-container-restarts-{now}",
            write=not verb.endswith("-probe"),
            fault=fault,
        )
    )
    for topic, body in container_restarts_events_for(
        result, container_restarts_load_topics()
    ):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"CONTAINER-RESTARTS outcome={result.outcome.value} fault={result.fault.value} "
        f"lane={result.census_lane} alarms={len(result.alarms)} "
        f"reasons={','.join(result.reasons) or 'none'}\n"
    )
    for reading in result.readings:
        out(f"CONTAINER {reading}\n")
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return _CONTAINER_RESTARTS_EXIT[EnumContainerRestartsOutcome.ALARM]
    return _CONTAINER_RESTARTS_EXIT[result.outcome]


def run_lab_pass_receipt(
    verb: str,
    fault: EnumLabPassReceiptFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerLabPassReceipt | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One lab-pass-receipt tick."""
    settings = overlay.lab_pass_receipt
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED lab_pass_receipt: the overlay has no lab_pass_receipt block; "
            "the other verbs do not need it\n"
        )
        return 1
    if handler is None:
        handler = build_lab_pass_receipt_handler(
            overlay, settings, lambda: datetime.now(UTC)
        )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelLabPassReceiptCommand(
            correlation_id=f"lab-alarm-{verb.replace('-probe', '')}-{now}",
            write=not verb.endswith("-probe"),
            fault=fault,
        )
    )
    for topic, body in lab_pass_receipt_events_for(
        result, lab_pass_receipt_load_topics()
    ):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"LAB-PASS-RECEIPT sha={result.sha} surface={result.surface} "
        f"outcome={result.outcome.value} fault={result.fault.value} "
        f"alarms={len(result.alarms)} reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return 2
    return _LAB_PASS_RECEIPT_EXIT[result.outcome]


def run_loaded_code_sha(
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerLoadedCodeSha | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One loaded-code-sha read; it never alarms and never delivers, so it keeps no fault and no probe form."""
    settings = overlay.lab_pass_receipt
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED loaded_code_sha: the overlay has no lab_pass_receipt block; "
            "the other verbs do not need it\n"
        )
        return 1
    if handler is None:
        handler = build_loaded_code_sha_handler(
            overlay, settings, lambda: datetime.now(UTC)
        )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelLoadedCodeShaRequest(correlation_id=f"lab-alarm-loaded-code-sha-{now}")
    )
    for topic, body in loaded_code_sha_events_for(
        result, loaded_code_sha_load_topics()
    ):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"LOADED-CODE-SHA sha={result.sha or 'none'} "
        f"surface={result.surface or 'none'} outcome={result.outcome.value}\n"
    )
    return _LOADED_CODE_SHA_EXIT[result.outcome]


def run_stale_indeterminate(
    verb: str,
    fault: EnumStaleIndeterminateFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerStaleIndeterminate | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One stale-indeterminate tick."""
    settings = overlay.stale_indeterminate
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED stale_indeterminate: the overlay has no stale_indeterminate block; "
            "the other verbs do not need it\n"
        )
        return 1
    if handler is None:
        handler = build_stale_indeterminate_handler(
            overlay, settings, lambda: datetime.now(UTC)
        )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelStaleIndeterminateCommand(
            correlation_id=f"lab-alarm-{verb.replace('-probe', '')}-{now}",
            write=not verb.endswith("-probe"),
            fault=fault,
        )
    )
    for topic, body in stale_indeterminate_events_for(
        result, stale_indeterminate_load_topics()
    ):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"STALE-INDETERMINATE conditions={','.join(result.conditions)} "
        f"outcome={result.outcome.value} fault={result.fault.value} "
        f"alarms={len(result.alarms)} reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return 2
    return _STALE_INDETERMINATE_EXIT[result.outcome]


# ---------------------------------------------------------------------------
# CLI verb table and parser registration
# ---------------------------------------------------------------------------

CONDITION_VERBS: tuple[str, ...] = (
    "lane-drift",
    "lane-drift-probe",
    "container-unhealthy",
    "container-unhealthy-probe",
    "lab-pass-receipt",
    "lab-pass-receipt-probe",
    "stale-indeterminate",
    "stale-indeterminate-probe",
    "loaded-code-sha",
    "container-restarts",
    "container-restarts-probe",
)

_FAULT_ENUMS: dict[
    str,
    type[EnumLaneCensusFault]
    | type[EnumLabPassReceiptFault]
    | type[EnumStaleIndeterminateFault]
    | type[EnumContainerRestartsFault],
] = {
    "lane-drift": EnumLaneCensusFault,
    "container-unhealthy": EnumLaneCensusFault,
    "lab-pass-receipt": EnumLabPassReceiptFault,
    "stale-indeterminate": EnumStaleIndeterminateFault,
    "container-restarts": EnumContainerRestartsFault,
}


def add_condition_parsers(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the nine condition subparsers on the CLI subparser action."""
    for base in (
        "lane-drift",
        "container-unhealthy",
        "lab-pass-receipt",
        "stale-indeterminate",
        "container-restarts",
    ):
        fault_enum = _FAULT_ENUMS[base]
        choices = [f.value for f in fault_enum]
        sub.add_parser(f"{base}", help=f"Run the {base} condition")
        sub.add_parser(f"{base}-probe", help=f"Probe the {base} condition")
        # Attach --fault to both the base and probe subparsers
        for verb in (f"{base}", f"{base}-probe"):
            parser = sub.choices[verb]
            parser.add_argument(
                "--fault",
                choices=choices,
                default="none",
                help="Fault to inject for this tick",
            )
    # The loaded-code-sha read only reads: no probe form and no --fault to inject
    sub.add_parser(
        "loaded-code-sha", help="Read the sha the lane's deploy agent has loaded"
    )
    sub.choices["loaded-code-sha"].set_defaults(fault="none")


def run_condition_verb(
    verb: str,
    fault_value: str,
    *,
    overlay: ModelLabAlarmOverlay,
) -> int:
    """Dispatch a condition verb to its run_* function."""
    base = verb.replace("-probe", "")
    if base == "lane-drift" or base == "container-unhealthy":
        return run_lane_census(verb, EnumLaneCensusFault(fault_value), overlay=overlay)
    if base == "lab-pass-receipt":
        return run_lab_pass_receipt(
            verb, EnumLabPassReceiptFault(fault_value), overlay=overlay
        )
    if base == "stale-indeterminate":
        return run_stale_indeterminate(
            verb, EnumStaleIndeterminateFault(fault_value), overlay=overlay
        )
    if base == "loaded-code-sha":
        return run_loaded_code_sha(overlay=overlay)
    if base == "container-restarts":
        return run_container_restarts(
            verb, EnumContainerRestartsFault(fault_value), overlay=overlay
        )
    sys.stderr.write(f"LAB-ALARM-REFUSED {verb}: unknown condition verb\n")
    return 1


# ---------------------------------------------------------------------------
# Journaling for pre-existing verbs
# ---------------------------------------------------------------------------

JOURNALED_EXIT_VERBS: tuple[str, ...] = (
    "dev-tag-heartbeat",
    "delegation-chain-canary",
    "work-ledger-projection-stale",
)

_OUTCOME_BY_EXIT: dict[int, str] = {
    0: "OK",
    2: "ALARM",
    3: "INDETERMINATE",
}


def journal_exit(
    verb: str,
    exit_code: int,
    *,
    overlay: ModelLabAlarmOverlay,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Append a verdict-record entry for journaled verbs that finished cleanly."""
    if verb not in JOURNALED_EXIT_VERBS:
        return exit_code
    if overlay.verdict_record is None:
        return exit_code
    if exit_code not in _OUTCOME_BY_EXIT:
        return exit_code
    record = FileRunRecords(Path(overlay.verdict_record.path))
    entry = {
        "finished_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reports": [
            {
                "condition": verb.replace("-", "_"),
                "outcome": _OUTCOME_BY_EXIT[exit_code],
                "evidence": f"exit code {exit_code}",
            }
        ],
    }
    try:
        record.append(entry)
    except RunRecordUnwritableError:
        return 4
    return exit_code
