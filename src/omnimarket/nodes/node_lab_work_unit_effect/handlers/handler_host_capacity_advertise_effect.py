# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostCapacityAdvertiseEffect -- this pool host's capacity, as an
advertisement (OMN-20105).

Canonical def-B handler: ``handle(request: ModelHostCapacityProbeRequest) ->
ModelHostCapacityAdvertisement``. It is the only reader of load, cores and
memory in the lab work path; placement reads the published advertisements.

``advertised_at`` is stamped here from the handler's clock, never taken from
the caller (the request has no such field). An unreadable reading raises
:class:`HostCapacityUnreadableError` and produces no advertisement: a reader
downstream sees the host's advertisements go stale, which placement treats as
``could_not_check``, never as an idle host.

:class:`HostAdvertiser` (OMN-21811) publishes those advertisements for the serve
process and judges whether the host has gone silent. On 2026-10-31 the CI bus
broker was down from about 10:05Z to 13:26Z; on every pool host one failed
publish ended the advertiser task with its exception never read, the process
and its command consumer stayed up, and the pool read no host for the rest of
the day. A failed publish is now logged and the next beat retries; a host that
has published nothing for the contract's ``capacity.silent_exit_seconds`` is
reported silent, and ``serve`` leaves non-zero so its supervisor restarts it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import RuntimeHostError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus, event_type_for
from omnimarket.handlers.host_capacity_overlay import load_host_capacity_limits
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_capacity import (
    ModelHostCapacityLimits,
    ModelHostCapacityReading,
    ModelHostCapacityRequest,
)
from omnimarket.nodes.node_host_capacity_compute.handlers.handler_host_capacity import (
    HandlerHostCapacity,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    HostReading,
    LocalHostReader,
    ProtocolHostReader,
)


class HandlerHostCapacityAdvertiseEffect:
    """EFFECT handler: read this host and return its advertisement."""

    def __init__(
        self,
        reader: ProtocolHostReader | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        limits: Callable[[], ModelHostCapacityLimits] = load_host_capacity_limits,
    ) -> None:
        self._reader = reader or LocalHostReader()
        self._now = now
        self._limits = limits

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelHostCapacityProbeRequest
    ) -> ModelHostCapacityAdvertisement:
        reading = self._reader.read(list(request.tools))
        max_units, record, refusal = self._slot_cap(request, reading)
        return ModelHostCapacityAdvertisement(
            host_name=request.host_name,
            cores=reading.cores,
            load1=reading.load1,
            mem_available_bytes=reading.mem_available_bytes,
            tools=list(reading.tools),
            running_units=request.running_units,
            max_units=max_units,
            rank_penalty=request.rank_penalty,
            advertised_at=self._now(),
            cadence_seconds=request.cadence_seconds,
            mem_total_bytes=reading.mem_total_bytes,
            swap_in_kb_s=reading.swap_in_kb_s,
            swap_out_kb_s=reading.swap_out_kb_s,
            slot_cap_record=record,
            capacity_refusal=refusal,
        )

    def _slot_cap(
        self, request: ModelHostCapacityProbeRequest, reading: HostReading
    ) -> tuple[int, str, str]:
        """The advertised slot cap, the line recording its inputs, and the refusal ('' when the host is admitted).

        The cap is the capacity node's, held under the serve process's worker pool (``request.max_units``, the
        most units it can run). A reading with no total memory, or no overlay to read the limits from, advertises
        the worker pool as before and says so in the record instead of a computed cap.
        """
        if reading.mem_total_bytes is None:
            return (
                request.max_units,
                f"fixed max_units={request.max_units}: total memory unread",
                "",
            )
        try:
            limits = self._limits()
        except (
            NodeOverlayError
        ) as exc:  # the advertisement says the cap was not computed
            logger.warning("host_capacity overlay unavailable: %s", exc)
            return (
                request.max_units,
                f"fixed max_units={request.max_units}: host_capacity overlay unavailable",
                "",
            )
        verdict = HandlerHostCapacity().handle(
            ModelHostCapacityRequest(
                reading=ModelHostCapacityReading(
                    host=request.host_name,
                    logical_cores=reading.cores,
                    mem_total_gb=reading.mem_total_bytes / 1024**3,
                    mem_avail_gb=reading.mem_available_bytes / 1024**3,
                    swap_in_kb_s=reading.swap_in_kb_s,
                    swap_out_kb_s=reading.swap_out_kb_s,
                    load_one_minute=reading.load1,
                    running_lanes=request.running_units,
                ),
                limits=limits,
            )
        )
        cap = max(1, min(request.max_units, verdict.slot_cap))
        return (
            cap,
            f"{verdict.record()} workers={request.max_units}",
            "; ".join(verdict.refusals),
        )


logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def silent_exit_seconds() -> int:
    """How long a host may publish nothing before it is silent: the node contract's ``capacity.silent_exit_seconds``."""
    return int(
        yaml.safe_load(CONTRACT_PATH.read_text())["capacity"]["silent_exit_seconds"]
    )


class HostAdvertiser:
    """Publishes one host's capacity advertisements and says when the host has published none for too long."""

    def __init__(
        self,
        host: str,
        clock: Callable[[], float] = time.monotonic,
        silent_seconds: int | None = None,
    ) -> None:
        self._host = host
        self._clock = clock
        self._silent_seconds = silent_seconds or silent_exit_seconds()
        self.failures = 0
        self.last_published = clock()
        self.armed = False
        self.reported = False

    def arm(self) -> None:
        """Start judging: a host started without an advertiser is never silent."""
        self.armed = True
        self.last_published = self._clock()

    async def publish(
        self,
        bus: ProtocolLabRunBus,
        topic: str,
        ad: ModelHostCapacityAdvertisement,
    ) -> bool:
        """Publish ``ad``; a failed publish is logged and answers False so the next beat retries."""
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=ad.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(topic),
        )
        body = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
        try:
            await bus.publish(topic, self._host.encode("utf-8"), body)
        except (RuntimeHostError, OSError, TimeoutError):
            self.failures += 1
            logger.exception(
                "lab-work host %s: capacity publish failed (%d so far), the next beat retries",
                self._host,
                self.failures,
            )
            return False
        self.last_published = self._clock()
        self.reported = False
        return True

    def silent(self) -> bool:
        """True once the advertiser is armed and has published nothing for the bound; logged once per silence.

        Its clock is its own (monotonic), never the host's injected unit clock, so a test that jumps the unit clock
        to age a held unit does not age the advertiser with it.
        """
        gap = self._clock() - self.last_published
        if not self.armed or gap <= self._silent_seconds:
            return False
        if not self.reported:
            self.reported = True
            logger.critical(
                "lab-work host %s: no capacity advertisement published for %ds (bound %ds, %d failed publishes); "
                "serve leaves non-zero so its supervisor restarts it",
                self._host,
                int(gap),
                self._silent_seconds,
                self.failures,
            )
        return True


__all__ = [
    "HandlerHostCapacityAdvertiseEffect",
    "HostAdvertiser",
    "silent_exit_seconds",
]
