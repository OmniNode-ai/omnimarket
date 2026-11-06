# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger durability: one probe row appended through the bus must read back from the durable store.

Piece 6 of the base-first rebuild plan (OMN-22662). The daily parity receipt says a day late whether
the file and the database projection agree. This probe asks the running system now: it appends one
probe row through the configured append route, then reads it back by its ``req=`` cell through the
configured read route (the lab read node over the database projection) until a deadline.

- The row reads back in time: WORKING, with the lag from the probe's stamp to the row's
  ``projected_at`` (else to the read that found it).
- The row reads back with a lag past the deadline (a slow read can find it after the deadline):
  NOT WORKING, ``durable-row-after-deadline``, with its lag.
- The append was accepted and no durable row came by the deadline: NOT WORKING,
  ``no-durable-row-by-deadline``. This is the plan's named fault, an append that returns with no
  durable row. Lag alone is this reading; a row that lands later is a late row, not a lost one.
- The append did not land (refused, no receipt, error): NOT WORKING, ``probe-append-<status>``.
- Every read failed: UNKNOWN, ``durable-read-failed-<why>``; a failed read is never an empty one.

Named break: an append route that answers ``accepted`` and publishes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import (
    ModelDurabilityProbeSettings,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

_PROBE = EnumHostProbe.LEDGER_DURABILITY
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=durability-probe"


def _reading(
    verdict: EnumReadingVerdict,
    outcome: EnumReadingOutcome,
    reason: str,
    cells: dict[str, str],
) -> ModelProbeReading:
    return ModelProbeReading(
        probe=_PROBE, verdict=verdict, outcome=outcome, reasons=(reason,), cells=cells
    )


def probe_row(
    settings: ModelDurabilityProbeSettings,
    *,
    host: str,
    actor: str,
    probe_id: str,
    stamp: datetime,
) -> str:
    """The probe row: one STATUS row on the probe lane, naming its host and probe id."""
    cells = [
        f"lane={settings.probe_lane}",
        _ROW_SOURCE_CELL,
        f"actor={actor}",
        "model=none",
        f"host={host}",
        f"probe_id={probe_id}",
        f"drill={'yes' if settings.drill else 'no'}",
        f"deadline_s={settings.deadline_seconds}",
        "ledger durability probe: this row must read back from the durable store by its deadline",
    ]
    return f"{stamp_text(stamp)} | {_ROW_TYPE} | " + " | ".join(cells) + "\n"


def read_durability(
    settings: ModelDurabilityProbeSettings,
    *,
    host: str,
    actor: str,
    appender: ProtocolReadingAppender,
    reader: ProtocolLedgerRowSource,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> ModelProbeReading:
    """Append one probe row, then poll the durable store for it until the deadline."""
    probe_id = new_id()
    request_id = new_id()
    stamp = clock().astimezone(UTC).replace(microsecond=0)
    cells = {
        "probe_req": request_id,
        "drill": "yes" if settings.drill else "no",
        "deadline_s": str(settings.deadline_seconds),
    }
    appended = appender.append(
        probe_row(settings, host=host, actor=actor, probe_id=probe_id, stamp=stamp),
        request_id,
    )
    cells["append"] = appended.status
    if not appended.ok:
        cells.update(durable="no", reads="0")
        return _reading(
            EnumReadingVerdict.NOT_WORKING,
            EnumReadingOutcome.READ,
            f"probe-append-{appended.status}",
            cells,
        )

    deadline = stamp + timedelta(seconds=settings.deadline_seconds)
    reads = 0
    answered = 0
    last_error = ""
    while True:
        now = clock().astimezone(UTC)
        reads += 1
        answer = reader.read(
            ModelLedgerQuery(
                since=stamp - timedelta(seconds=1),
                until=now + timedelta(seconds=1),
                row_type=_ROW_TYPE,
                lane=settings.probe_lane,
                term=f"req={request_id}",
            )
        )
        if answer.error is not None:
            last_error = answer.error
        else:
            answered += 1
            found = next(
                (row for row in answer.rows if f"probe_id={probe_id}" in row.raw), None
            )
            if found is not None:
                # A read can take up to its timeout: without a projected_at the row is known
                # durable only when the read that found it ended, never when it started.
                durable_at = found.ingested_at or clock().astimezone(UTC)
                lag = max(0, round((durable_at - stamp).total_seconds()))
                if lag > settings.deadline_seconds:
                    cells.update(
                        durable="late", durable_lag_s=str(lag), reads=str(reads)
                    )
                    return _reading(
                        EnumReadingVerdict.NOT_WORKING,
                        EnumReadingOutcome.READ,
                        "durable-row-after-deadline",
                        cells,
                    )
                cells.update(durable="yes", durable_lag_s=str(lag), reads=str(reads))
                return _reading(
                    EnumReadingVerdict.WORKING,
                    EnumReadingOutcome.READ,
                    "durable-row-read-back",
                    cells,
                )
        if now >= deadline:
            break
        sleep(
            float(min(settings.poll_interval_seconds, (deadline - now).total_seconds()))
        )

    cells.update(durable="no", reads=str(reads), answered=str(answered))
    if answered == 0:
        return _reading(
            EnumReadingVerdict.UNKNOWN,
            EnumReadingOutcome.SOURCE_UNREADABLE,
            f"durable-read-failed-{reason_token(last_error)}",
            cells,
        )
    return _reading(
        EnumReadingVerdict.NOT_WORKING,
        EnumReadingOutcome.READ,
        "no-durable-row-by-deadline",
        cells,
    )
