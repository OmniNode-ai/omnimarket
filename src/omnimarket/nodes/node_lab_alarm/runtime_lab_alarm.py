# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_alarm <verb>``.

``broker-lag``  read the declared nodes' live consumer groups and keep the sample (what a schedule runs).
``probe``       the same read, keeping nothing.
``schedule``    install | uninstall | status | run-now, for a host in the overlay's schedule hosts.
``effects-held``        read the delegate-skill consumer groups, keep the hold clock and open one lane when no live
                        member has held past the bound (what a schedule runs).
``effects-held-probe``  the same read, keeping nothing and opening nothing.

``broker-listener``        dial the dev broker's external listener and deliver an alarm when every address refuses.
``broker-listener-probe``  the same dial, delivering nothing.

``--fault`` injects a drill fault (planted-lag, unknown-node, manifest-down); a drill never keeps its sample.

Exit codes: 0 OK, 2 ALARM, 3 INDETERMINATE (a group or the manifest could not be read, or no baseline yet),
1 the command was refused (a schedule verb exits with its own result's code), 4 the run record the floor reads could not be written.
An alarm delivery (the overlay's ``alarm_delivery``) that failed or could not run exits 2, as an alarm that was not delivered is one.
1 the command was refused. An effects-held ALARM whose lane did not start exits 2 and names ``escalation=failed``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryDispatcher,
    InMemoryEffectsGroups,
    InMemoryGrowthStreaks,
    InMemoryHeldState,
    InMemoryLag,
    InMemoryListenerProbe,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    AdminLag,
    CommandLaneDispatcher,
    FileGrowthStreaks,
    FileHeldState,
    FileManifestCache,
    FileRunRecords,
    FileSamples,
    HttpManifest,
    KafkaEffectsGroups,
    KafkaLag,
    TcpApiVersionsProbe,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_listener import (
    HandlerBrokerListener,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_listener import (
    events_for as broker_listener_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_listener import (
    load_topics as broker_listener_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_effects_held import (
    HandlerEffectsHeld,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_effects_held import (
    events_for as effects_held_events_for,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_effects_held import (
    load_topics as effects_held_load_topics,
)
from omnimarket.nodes.node_lab_alarm.handlers.schedule import (
    LabAlarmScheduler,
    ModelLabAlarmScheduleResult,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_broker_listener import (
    EnumBrokerListenerFault,
    EnumBrokerListenerOutcome,
    ModelBrokerListenerCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    EnumEffectsHeldFault,
    EnumEffectsHeldOutcome,
    ModelEffectsHeldCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelBrokerListenerSettings,
    ModelEffectsHeldSettings,
    ModelLabAlarmOverlay,
    load_overlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolGroupLagReader,
    ProtocolListenerProbe,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    load_overlay as load_delivery_overlay,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler as build_delivery_handler,
)

_EXIT_RECORD_UNWRITABLE = 4
_EXIT = {
    EnumBrokerLagOutcome.OK: 0,
    EnumBrokerLagOutcome.ALARM: 2,
    EnumBrokerLagOutcome.INDETERMINATE: 3,
}
_EFFECTS_EXIT = {
    EnumEffectsHeldOutcome.OK: 0,
    EnumEffectsHeldOutcome.ALARM: 2,
    EnumEffectsHeldOutcome.INDETERMINATE: 3,
}


_LISTENER_EXIT = {
    EnumBrokerListenerOutcome.OK: 0,
    EnumBrokerListenerOutcome.ALARM: 2,
    EnumBrokerListenerOutcome.INDETERMINATE: 3,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _delivery_for(overlay: ModelLabAlarmOverlay) -> HandlerLabAlarmDeliver | None:
    """The alarm delivery the overlay turns on; its own overlay must then load, or the run is refused."""
    if overlay.alarm_delivery is None or not overlay.alarm_delivery.enabled:
        return None
    return build_delivery_handler(load_delivery_overlay())


def _inmemory_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=overlay,
        manifest=InMemoryManifest({"contracts": []}),
        lag=InMemoryLag({}),
        samples=InMemorySamples(),
        streaks=InMemoryGrowthStreaks(),
        records=InMemoryRunRecords(),
        clock=_utc_now,
        delivery=_delivery_for(overlay),
    )


def _runtime_handler_over(
    overlay: ModelLabAlarmOverlay, lag: ProtocolGroupLagReader
) -> HandlerBrokerLag:
    """The runtime backends differ only in how they read a group's lag."""
    resilience = overlay.manifest.resilience
    return HandlerBrokerLag(
        overlay=overlay,
        manifest=HttpManifest(
            overlay.manifest.url,
            overlay.manifest.timeout_seconds,
            attempts=resilience.attempts if resilience else 1,
            retry_delay_seconds=resilience.retry_delay_seconds if resilience else 0.0,
        ),
        manifest_cache=(
            FileManifestCache(Path(resilience.cache_path)) if resilience else None
        ),
        lag=lag,
        samples=FileSamples(Path(overlay.lag.state_path)),
        streaks=(
            FileGrowthStreaks(Path(overlay.lag.growth.streak_path))
            if overlay.lag.growth
            else None
        ),
        records=FileRunRecords(Path(overlay.lag.record_path)),
        clock=_utc_now,
        delivery=_delivery_for(overlay),
    )


def _runtime_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    return _runtime_handler_over(
        overlay, KafkaLag(overlay.bus.lane, overlay.lag.timeout_seconds)
    )


def _runtime_admin_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    if overlay.bus.bootstrap_servers is None:
        raise ValueError("backend runtime_admin needs bootstrap_servers")
    return _runtime_handler_over(
        overlay, AdminLag(overlay.bus.bootstrap_servers, overlay.lag.timeout_seconds)
    )


_BACKEND_BUILDERS: dict[str, Callable[[ModelLabAlarmOverlay], HandlerBrokerLag]] = {
    "inmemory": _inmemory_handler,
    "runtime": _runtime_handler,
    "runtime_admin": _runtime_admin_handler,
}


def build_handler(overlay: ModelLabAlarmOverlay) -> HandlerBrokerLag:
    """The handler wired to the overlay's backend."""
    return _BACKEND_BUILDERS[overlay.bus.backend](overlay)


def run(
    verb: str,
    fault: EnumLabAlarmFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerBrokerLag | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    handler = handler or build_handler(overlay)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelBrokerLagCommand(
            correlation_id=f"lab-alarm-broker-lag-{now}",
            write=verb == "broker-lag",
            fault=fault,
        )
    )
    for topic, body in events_for(result, load_topics()):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    for reading in result.readings:
        out(
            f"GROUP {reading.group} state={reading.state.value} lag={reading.lag} "
            f"previous={reading.previous_lag}\n"
        )
    out(
        f"BROKER-LAG outcome={result.outcome.value} declared={len(result.declared_groups)} "
        f"fault={result.fault.value} sample_kept={result.sample_kept} "
        f"alarms={len(result.alarms)} reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if result.failure.startswith("record-unwritable"):
        return _EXIT_RECORD_UNWRITABLE
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return _EXIT[EnumBrokerLagOutcome.ALARM]
    return _EXIT[result.outcome]


def run_schedule(
    action: str,
    host: str,
    *,
    overlay: ModelLabAlarmOverlay,
    scheduler: LabAlarmScheduler | None = None,
    omni_root: Path | None = None,
    overlay_root: Path | None = None,
    dry_run: bool = False,
    out: Callable[[str], object] | None = None,
    err: Callable[[str], object] | None = None,
) -> int:
    """One schedule verb on one declared host; the exit code is the result's."""
    scheduler = scheduler or LabAlarmScheduler(overlay=overlay, home=Path.home())
    result: ModelLabAlarmScheduleResult
    if action == "install":
        if omni_root is None or overlay_root is None:
            result = ModelLabAlarmScheduleResult(
                exit_code=1,
                stderr="install needs the registry root (OMNI_HOME) and an overlay root\n",
            )
        else:
            result = scheduler.install(
                host, omni_root=omni_root, overlay_root=overlay_root, dry_run=dry_run
            )
    elif action == "uninstall":
        result = scheduler.uninstall(host)
    elif action == "run-now":
        result = scheduler.run_now(host)
    else:
        result = scheduler.status(host)
    (out or sys.stdout.write)(result.stdout)
    (err or sys.stderr.write)(result.stderr)
    return result.exit_code


def _first_overlay_root(environ: Mapping[str, str]) -> Path | None:
    roots = [
        r for r in environ.get("ONEX_SKILL_OVERLAY_ROOTS", "").split(os.pathsep) if r
    ]
    return Path(roots[0]) if roots else None


def _inmemory_effects_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelEffectsHeldSettings,
    clock: Callable[[], datetime],
) -> HandlerEffectsHeld:
    return HandlerEffectsHeld(
        settings=settings,
        groups=InMemoryEffectsGroups(()),
        state=InMemoryHeldState(),
        dispatcher=InMemoryDispatcher(),
        clock=clock,
    )


def _runtime_effects_handler(
    overlay: ModelLabAlarmOverlay,
    settings: ModelEffectsHeldSettings,
    clock: Callable[[], datetime],
) -> HandlerEffectsHeld:
    return HandlerEffectsHeld(
        settings=settings,
        groups=KafkaEffectsGroups(
            overlay.bus.lane,
            settings.timeout_seconds,
            settings.group_prefix,
            settings.group_suffix,
        ),
        state=FileHeldState(Path(settings.state_path)),
        dispatcher=CommandLaneDispatcher(settings.escalation),
        clock=clock,
    )


_EFFECTS_BACKEND_BUILDERS: dict[
    str,
    Callable[
        [ModelLabAlarmOverlay, ModelEffectsHeldSettings, Callable[[], datetime]],
        HandlerEffectsHeld,
    ],
] = {
    "inmemory": _inmemory_effects_handler,
    "runtime": _runtime_effects_handler,
}


def run_effects_held(
    verb: str,
    fault: EnumEffectsHeldFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerEffectsHeld | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One effects-held tick."""
    settings = overlay.effects_held
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED effects_held: the overlay has no effects_held block; "
            "the broker lag verbs do not need it\n"
        )
        return 1
    handler = handler or _EFFECTS_BACKEND_BUILDERS[overlay.bus.backend](
        overlay, settings, lambda: datetime.now(UTC)
    )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelEffectsHeldCommand(
            correlation_id=f"lab-alarm-effects-held-{now}",
            write=verb == "effects-held",
            fault=fault,
        )
    )
    for topic, body in effects_held_events_for(result, effects_held_load_topics()):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    for reading in result.readings:
        out(f"GROUP {reading.group} state={reading.state} members={reading.members}\n")
    out(
        f"EFFECTS-HELD outcome={result.outcome.value} fault={result.fault.value} "
        f"held_since={result.held_since or 'none'} held_for={result.held_for_seconds:.0f}s "
        f"bound={result.bound_seconds:.0f}s escalation={result.escalation.value} "
        f"state_kept={result.state_kept} reasons={','.join(result.reasons) or 'none'}\n"
    )
    return _EFFECTS_EXIT[result.outcome]


_LISTENER_PROBES: dict[str, Callable[[], ProtocolListenerProbe]] = {
    "inmemory": InMemoryListenerProbe,
    "runtime": TcpApiVersionsProbe,
}


def build_broker_listener_handler(
    overlay: ModelLabAlarmOverlay, settings: ModelBrokerListenerSettings
) -> HandlerBrokerListener:
    """The listener handler wired to the overlay's backend: the TCP probe on the runtime backend, fixtures in memory."""
    return HandlerBrokerListener(
        settings=settings,
        probe=_LISTENER_PROBES[overlay.bus.backend](),
        delivery=_delivery_for(overlay),
    )


def run_broker_listener(
    verb: str,
    fault: EnumBrokerListenerFault,
    *,
    overlay: ModelLabAlarmOverlay,
    handler: HandlerBrokerListener | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """One broker-listener tick."""
    settings = overlay.broker_listener
    if settings is None:
        sys.stderr.write(
            "LAB-ALARM-REFUSED broker_listener: the overlay has no broker_listener block; "
            "the other verbs do not need it\n"
        )
        return 1
    handler = handler or build_broker_listener_handler(overlay, settings)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelBrokerListenerCommand(
            correlation_id=f"lab-alarm-broker-listener-{now}",
            write=verb == "broker-listener",
            fault=fault,
        )
    )
    for topic, body in broker_listener_events_for(
        result, broker_listener_load_topics()
    ):
        out(f"EVENT topic={topic} correlation_id={body['correlation_id']}\n")
    out(
        f"BROKER-LISTENER outcome={result.outcome.value} fault={result.fault.value} "
        f"bootstrap={result.bootstrap_servers} alarms={len(result.alarms)} "
        f"reasons={','.join(result.reasons) or 'none'}\n"
    )
    for alarm in result.alarms:
        out(f"ALARM {alarm}\n")
    for entry in result.delivery:
        out(f"DELIVERY {entry}\n")
    if "delivery-failed" in result.failure or "delivery-unavailable" in result.failure:
        return _LISTENER_EXIT[EnumBrokerListenerOutcome.ALARM]
    return _LISTENER_EXIT[result.outcome]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lab_alarm")
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("broker-lag", "probe"):
        reader = sub.add_parser(verb)
        reader.add_argument(
            "--fault", choices=[f.value for f in EnumLabAlarmFault], default="none"
        )
    for verb in ("effects-held", "effects-held-probe"):
        held = sub.add_parser(verb)
        held.add_argument(
            "--fault", choices=[f.value for f in EnumEffectsHeldFault], default="none"
        )
    for verb in ("broker-listener", "broker-listener-probe"):
        listener = sub.add_parser(verb)
        listener.add_argument(
            "--fault",
            choices=[f.value for f in EnumBrokerListenerFault],
            default="none",
        )
    schedule = sub.add_parser("schedule")
    schedule.add_argument(
        "action", choices=("install", "uninstall", "status", "run-now")
    )
    schedule.add_argument("--host", required=True)
    schedule.add_argument("--dry-run", action="store_true")
    schedule.add_argument(
        "--overlay-root",
        type=Path,
        default=None,
        help="the root the unit reads overlays from",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler: HandlerBrokerLag | None = None
    try:
        overlay = load_overlay()
        if args.verb in ("broker-lag", "probe"):
            handler = build_handler(overlay)
    except NodeOverlayError as exc:
        sys.stderr.write(f"LAB-ALARM-REFUSED {exc}\n")
        return 1
    if args.verb == "schedule":
        registry_root = os.environ.get("OMNI_HOME")
        return run_schedule(
            args.action,
            args.host,
            overlay=overlay,
            omni_root=Path(registry_root) if registry_root else None,
            overlay_root=args.overlay_root or _first_overlay_root(os.environ),
            dry_run=args.dry_run,
        )
    if args.verb.startswith("broker-listener"):
        return run_broker_listener(
            args.verb, EnumBrokerListenerFault(args.fault), overlay=overlay
        )
    if args.verb.startswith("effects-held"):
        return run_effects_held(
            args.verb, EnumEffectsHeldFault(args.fault), overlay=overlay
        )
    return run(
        args.verb, EnumLabAlarmFault(args.fault), overlay=overlay, handler=handler
    )


if __name__ == "__main__":
    raise SystemExit(main())
