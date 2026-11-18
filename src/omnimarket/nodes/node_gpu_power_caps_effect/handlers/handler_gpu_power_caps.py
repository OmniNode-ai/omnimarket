# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GPU power caps effect handler: ``handle(request) -> result`` (OMN-24042).

Reads the host's GPU power caps from the gpu_power_caps settings, finds each card by product name
over the host's declared transport, and applies a limit only where the card's limit differs from
the cap. Anything the settings or the overlay do not cover is refused with a typed result before
a write starts; every applied limit is read back from the card.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_net_units_overlay import (
    ModelNetUnitDeclaration,
    ModelNetUnitsOverlay,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.backend_in_memory import (
    BackendGpuPowerCapsInMemory,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.backend_subprocess import (
    BackendGpuPowerCapsSubprocess,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.dispatch_lab_work import (
    DispatchGpuPowerCapsLabWork,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.protocols import (
    ProtocolGpuPowerCapsBackend,
    ProtocolLabWorkSender,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.sender_lab_work_bus import (
    SenderLabWorkBus,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsBackend,
    EnumGpuPowerCapsCardAction,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuCap,
    ModelGpuCard,
    ModelGpuPowerCapsCardResult,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
    ModelGpuSetRun,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    NODE_NAME,
    ModelGpuPowerCapsOverlay,
)


def select_backend(overlay: ModelGpuPowerCapsOverlay) -> ProtocolGpuPowerCapsBackend:
    """The backend the overlay names. The lab_work backend has none of its own: its host runs this node's command line."""
    if overlay.backend is EnumGpuPowerCapsBackend.IN_MEMORY:
        return BackendGpuPowerCapsInMemory(overlay)
    if overlay.backend is EnumGpuPowerCapsBackend.LAB_WORK:
        raise ValueError(
            "the lab_work backend sends the run to the host; it has no local backend"
        )
    return BackendGpuPowerCapsSubprocess(
        overlay.read_argv,
        overlay.set_argv,
        overlay.read_timeout_seconds,
        overlay.set_timeout_seconds,
    )


def caps_of_unit(
    unit: ModelNetUnitDeclaration, name_prefix: str, watts_prefix: str
) -> tuple[ModelGpuCap, ...]:
    """The caps a unit declares: a name key and its watts key share the pair id after the prefix."""
    caps: list[ModelGpuCap] = []
    card_names: set[str] = set()
    for file in unit.files:
        for key in file.values:
            if key.startswith(watts_prefix) and (
                name_prefix + key[len(watts_prefix) :] not in file.values
            ):
                raise ValueError("a watts key has no matching name key")
        for key in file.values:
            if not key.startswith(name_prefix):
                continue
            watts_key = watts_prefix + key[len(name_prefix) :]
            if watts_key not in file.values:
                raise ValueError("a name key has no matching watts key")
            watts_text = file.values[watts_key]
            if (
                not (watts_text.isascii() and watts_text.isdigit())
                or int(watts_text) <= 0
            ):
                raise ValueError("a watts value is not a positive base-10 integer")
            cap = ModelGpuCap(name=file.values[key], watts=int(watts_text))
            if cap.name in card_names:
                raise ValueError("a card name is declared twice")
            card_names.add(cap.name)
            caps.append(cap)
    if not caps:
        raise ValueError("no caps declared")
    return tuple(caps)


class HandlerGpuPowerCaps:
    """All four parts are injected by tests and the CLI; the runtime boots it bare and the
    handler reads the overlay and builds the backend it names on the first request. The caps
    settings are re-read on EVERY request, so a changed value applies without a restart. With
    the lab_work backend the run is sent to the host and nothing is read or written here."""

    def __init__(
        self,
        overlay: ModelGpuPowerCapsOverlay | None = None,
        backend: ProtocolGpuPowerCapsBackend | None = None,
        caps_loader: Callable[[], ModelNetUnitsOverlay] | None = None,
        sender: ProtocolLabWorkSender | None = None,
    ) -> None:
        self._overlay_value = overlay
        self._backend_value = backend
        self._caps_loader_value = caps_loader
        self._sender_value = sender

    @property
    def _overlay(self) -> ModelGpuPowerCapsOverlay:
        if self._overlay_value is None:
            self._overlay_value = load_structured_overlay(
                NODE_NAME, ModelGpuPowerCapsOverlay
            )
        return self._overlay_value

    @property
    def _backend(self) -> ProtocolGpuPowerCapsBackend:
        if self._backend_value is None:
            self._backend_value = select_backend(self._overlay)
        return self._backend_value

    @property
    def _caps_loader(self) -> Callable[[], ModelNetUnitsOverlay]:
        if self._caps_loader_value is None:

            def load_caps() -> ModelNetUnitsOverlay:
                return load_structured_overlay(
                    self._overlay.caps_overlay_id, ModelNetUnitsOverlay
                )

            self._caps_loader_value = load_caps
        return self._caps_loader_value

    @property
    def _sender(self) -> ProtocolLabWorkSender:
        if self._sender_value is None:
            lab_work = self._overlay.lab_work
            if lab_work is None:
                raise ValueError("the overlay has no lab_work block")
            self._sender_value = SenderLabWorkBus(
                lab_work.bus_lane,
                lab_work.workspace_root,
                kafka_bootstrap=lab_work.kafka_bootstrap,
            )
        return self._sender_value

    async def handle(
        self, request: ModelGpuPowerCapsRequest
    ) -> ModelGpuPowerCapsResult:
        # The send blocks until the host's receipt (and the subprocess backend until nvidia-smi answers); the runtime calls
        # handle on its event loop, so the blocking part runs off it.
        return await asyncio.to_thread(self._handle_blocking, request)

    def _handle_blocking(
        self, request: ModelGpuPowerCapsRequest
    ) -> ModelGpuPowerCapsResult:
        overlay = self._overlay
        if overlay.backend is EnumGpuPowerCapsBackend.LAB_WORK:
            return DispatchGpuPowerCapsLabWork(overlay, self._sender).dispatch(request)
        host = overlay.host(request.host)
        if host is None:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.HOST_UNKNOWN,
                "no row for this host in the overlay",
            )
        try:
            caps_overlay = self._caps_loader()
        except NodeOverlayError as exc:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.CAPS_UNREADABLE,
                f"the gpu power caps settings could not be read: {type(exc).__name__}",
            )
        unit = caps_overlay.units.get(host.caps_unit)
        if unit is None:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.CAPS_UNREADABLE,
                f"the settings document declares no unit named {host.caps_unit}",
            )
        if request.host not in unit.hosts:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.CAPS_UNREADABLE,
                "the host is not listed in the unit's hosts",
            )
        try:
            caps = caps_of_unit(
                unit, overlay.name_value_prefix, overlay.watts_value_prefix
            )
        except ValueError as exc:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.CAPS_UNREADABLE,
                f"the caps of the unit are malformed: {exc}",
            )
        cards = self._backend.read_cards(host)
        if cards is None:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.GPUS_UNREADABLE,
                "the cards on the host could not be read; nothing was changed",
            )
        rows = tuple(
            ModelGpuPowerCapsCardResult(name=cap.name, wanted_watts=cap.watts)
            for cap in caps
        )
        matches_by_name: dict[str, list[ModelGpuCard]] = {}
        for card in cards:
            matches_by_name.setdefault(card.name, []).append(card)
        for cap in caps:
            matches = matches_by_name.get(cap.name, [])
            if not matches:
                return self._refused(
                    request,
                    EnumGpuPowerCapsRefusal.CARD_NOT_FOUND,
                    f"the host has no card named {cap.name}",
                    rows,
                )
            if len(matches) > 1:
                return self._refused(
                    request,
                    EnumGpuPowerCapsRefusal.CARD_AMBIGUOUS,
                    f"the host has more than one card named {cap.name}",
                    rows,
                )
            card = matches[0]
            if not card.min_limit_watts <= cap.watts <= card.max_limit_watts:
                return self._refused(
                    request,
                    EnumGpuPowerCapsRefusal.WATTS_OUT_OF_RANGE,
                    f"the cap wants {cap.watts} W but {cap.name} allows "
                    f"{card.min_limit_watts} to {card.max_limit_watts} W",
                    rows,
                )
        result_rows: list[ModelGpuPowerCapsCardResult] = []
        for cap in caps:
            card = matches_by_name[cap.name][0]
            before = card.power_limit_watts
            if before == cap.watts:
                result_rows.append(
                    ModelGpuPowerCapsCardResult(
                        name=cap.name,
                        wanted_watts=cap.watts,
                        index=card.index,
                        before_watts=before,
                        after_watts=before,
                        action=EnumGpuPowerCapsCardAction.UNCHANGED,
                    )
                )
                continue
            run = self._backend.set_limit(host, card.index, cap.watts)
            cards_after = self._backend.read_cards(host)
            after = next(
                (
                    read_back.power_limit_watts
                    for read_back in cards_after or ()
                    if read_back.index == card.index
                ),
                None,
            )
            if run.exit_code == 0 and not run.timed_out and after == cap.watts:
                action = EnumGpuPowerCapsCardAction.APPLIED
                detail = None
            else:
                action = EnumGpuPowerCapsCardAction.FAILED
                detail = self._failure_detail(run, after, cap.watts)
            result_rows.append(
                ModelGpuPowerCapsCardResult(
                    name=cap.name,
                    wanted_watts=cap.watts,
                    index=card.index,
                    before_watts=before,
                    after_watts=after,
                    action=action,
                    detail=detail,
                )
            )
        failed = any(
            row.action is EnumGpuPowerCapsCardAction.FAILED for row in result_rows
        )
        return ModelGpuPowerCapsResult(
            host=request.host,
            outcome=EnumGpuPowerCapsOutcome.FAILED
            if failed
            else EnumGpuPowerCapsOutcome.OK,
            cards=tuple(result_rows),
        )

    @staticmethod
    def _failure_detail(run: ModelGpuSetRun, after: int | None, wanted: int) -> str:
        if run.timed_out:
            return "the set timed out"
        if run.exit_code is None:
            return f"the set could not be run: {run.output_tail}"
        if run.exit_code != 0:
            return f"the set exited {run.exit_code}: {run.output_tail}"
        if after is None:
            return "the set exited 0 but the limit could not be read back"
        return f"the set exited 0 but the card reads back {after} W, wanted {wanted} W"

    @staticmethod
    def _refused(
        request: ModelGpuPowerCapsRequest,
        refusal: EnumGpuPowerCapsRefusal,
        detail: str,
        cards: tuple[ModelGpuPowerCapsCardResult, ...] = (),
    ) -> ModelGpuPowerCapsResult:
        return ModelGpuPowerCapsResult(
            host=request.host,
            outcome=EnumGpuPowerCapsOutcome.REFUSED,
            refusal=refusal,
            detail=detail,
            cards=cards,
        )
