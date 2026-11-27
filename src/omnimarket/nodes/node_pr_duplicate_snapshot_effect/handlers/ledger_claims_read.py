# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate guard's claims read: two bounded reads, each asked once more when it fails, never a skip (OMN-26433, OMN-26387).

The guard's fold keeps a CLAIM live until a later TERMINAL or RELEASE, while its lane has written any row in the last 0.75
hours. The rows that fold needs are therefore every row of the last hour (they name the live lanes) and the CLAIM, TERMINAL and
RELEASE rows of those lanes over the window. Reading every row of 168 hours (about 84,000 rows, 84 reply pages) cannot answer when
each reply page takes the dev broker 55 to 137 seconds (2026-11-22), and it did not finish in 400 seconds on h053 even when the
one hour read answered in 11 seconds (2026-11-24); the one hour read is about 580 rows and the live lanes' claim rows are about
200, a page each. Both reads go through the ledger reader node and none opens the ledger file. A read that fails or times out
(``LedgerReadError``) is asked once more, the same read, because the answerer stalls for tens of seconds and then flushes; when
the retry fails too, or the declared total budget is spent, the error reaches the caller, which refuses unchecked. The waits and
the budget are the node contract's (``ModelLedgerClaimsReadConfig``), and the clock is the caller's.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path

import yaml

from omnimarket.handlers.handler_ledger_claims import claim_lane
from omnimarket.handlers.ledger_row_grammar import LANE_TOKEN_RE
from omnimarket.models.model_ledger_claims_read import ModelLedgerClaimsReadConfig
from omnimarket.protocols.protocol_ledger_rows_reader import (
    LedgerReadError,
    ProtocolLedgerRowsReader,
)

logger = logging.getLogger(__name__)

__all__ = ["load_ledger_read_config", "read_claims_text"]


@cache
def load_ledger_read_config() -> ModelLedgerClaimsReadConfig:
    """The claims read's reader, windows and waits from the snapshot node's own contract."""
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    return ModelLedgerClaimsReadConfig.model_validate(contract["config"]["ledger_read"])


def _bounded(
    read: Callable[[ProtocolLedgerRowsReader], tuple[str, ...]],
    reader_for: Callable[[float], ProtocolLedgerRowsReader],
    config: ModelLedgerClaimsReadConfig,
    clock: Callable[[], float],
    started: float,
) -> tuple[str, ...]:
    """One read, and once more when it fails: each wait is its declared one or what is left of the total budget."""
    failures: list[str] = []
    for declared in (config.primary_timeout_s, config.retry_timeout_s):
        left = config.total_budget_s - (clock() - started)
        if left <= 0:
            failures.append(f"the read budget of {config.total_budget_s:g} s is spent")
            break
        try:
            return read(reader_for(min(declared, left)))
        except LedgerReadError as failure:
            logger.warning("claims read failed (%s)", failure)
            failures.append(str(failure))
    raise LedgerReadError("; ".join(failures))


def read_claims_text(
    reader_for: Callable[[float], ProtocolLedgerRowsReader],
    config: ModelLedgerClaimsReadConfig | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """The ledger rows the claim fold reads; ``LedgerReadError`` when a read and its retry both fail.

    ``reader_for`` gives a reader that waits at most the seconds it is passed (the declared bound of the read) and ``clock``
    reads seconds, so the total budget is the caller's to measure.
    """
    config = config or load_ledger_read_config()
    started = clock()

    def bounded(
        read: Callable[[ProtocolLedgerRowsReader], tuple[str, ...]],
    ) -> tuple[str, ...]:
        return _bounded(read, reader_for, config, clock, started)

    def heartbeat_read(reader: ProtocolLedgerRowsReader) -> tuple[str, ...]:
        rows = reader.rows(
            reader=config.reader, window_hours=config.heartbeat_window_hours
        )
        if not any(claim_lane(row) for row in rows):
            # A ledger always has a row in its last hour; an answer with none is not a read of it.
            raise LedgerReadError(
                f"the last {config.heartbeat_window_hours} hours held no lane row"
            )
        return rows

    heartbeat = bounded(heartbeat_read)
    # Only a lane token can be named: the work-ledger query refuses a request whose `lanes` hold anything else (OMN-24562). A hook's
    # FRICTION row named `wt:<repo>/<worktree>` on 2026-11-26, which made every claims read exit 69; such a cell holds no CLAIM.
    lanes = sorted(
        {
            lane
            for row in heartbeat
            if (lane := claim_lane(row)) and LANE_TOKEN_RE.match(lane)
        }
    )
    claims = bounded(
        lambda reader: reader.rows(
            reader=config.reader,
            window_hours=config.window_hours,
            kinds=config.claim_kinds,
            lanes=lanes,
        )
    )
    # A recent CLAIM is in both reads; a row listed twice would be a claim counted twice.
    return "\n".join(dict.fromkeys((*claims, *heartbeat)))
