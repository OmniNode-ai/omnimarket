# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate guard's claims read: a bounded primary read, a declared whole-window fallback, never a skip (OMN-26433).

The guard's fold keeps a CLAIM live until a later TERMINAL or RELEASE, while its lane has written any row in the last 0.75
hours. The rows that fold needs are therefore every row of the last hour (they name the live lanes) and the CLAIM, TERMINAL and
RELEASE rows of those lanes over the window. Reading every row of 168 hours (about 84,000 rows, 84 reply pages) cannot answer when
each reply page takes the dev broker 55 to 137 seconds (2026-11-22); the one hour read is about 580 rows and the live lanes' claim
rows are about 200, a page each. Both reads go through the ledger reader node and none opens the ledger file. A read that
fails or times out is ``LedgerReadError``: the fallback is tried, and when it fails too the caller refuses unchecked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path

import yaml

from omnimarket.handlers.handler_ledger_claims import claim_lane
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


def _primary(
    reader: ProtocolLedgerRowsReader, config: ModelLedgerClaimsReadConfig
) -> tuple[str, ...]:
    """Every row of the heartbeat hours, then the claim rows of the lanes those rows name."""
    heartbeat = reader.rows(
        reader=config.reader, window_hours=config.heartbeat_window_hours
    )
    lanes = sorted({lane for row in heartbeat if (lane := claim_lane(row))})
    if not lanes:
        # A ledger always has a row in its last hour; an answer with none is not a read of it.
        raise LedgerReadError(
            f"the last {config.heartbeat_window_hours} hours held no lane row"
        )
    claims = reader.rows(
        reader=config.reader,
        window_hours=config.window_hours,
        kinds=config.claim_kinds,
        lanes=lanes,
    )
    # A recent CLAIM is in both reads; a row listed twice would be a claim counted twice.
    return tuple(dict.fromkeys((*claims, *heartbeat)))


def read_claims_text(
    reader_for: Callable[[float], ProtocolLedgerRowsReader],
    config: ModelLedgerClaimsReadConfig | None = None,
) -> str:
    """The ledger rows the claim fold reads, from the primary read or the fallback; ``LedgerReadError`` when neither answers.

    ``reader_for`` gives a reader that waits at most the seconds it is passed (the declared bound of the tier).
    """
    config = config or load_ledger_read_config()
    try:
        return "\n".join(_primary(reader_for(config.primary_timeout_s), config))
    except LedgerReadError as primary_failure:
        logger.warning(
            "claims read failed (%s); reading every row of %d hours",
            primary_failure,
            config.window_hours,
        )
        try:
            rows = reader_for(config.fallback_timeout_s).rows(
                reader=config.reader, window_hours=config.window_hours
            )
        except LedgerReadError as fallback_failure:
            raise LedgerReadError(
                f"{primary_failure}; the whole-window fallback read: {fallback_failure}"
            ) from fallback_failure
    return "\n".join(rows)
