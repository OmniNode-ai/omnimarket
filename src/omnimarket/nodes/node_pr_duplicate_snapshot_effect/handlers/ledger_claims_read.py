# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate guard's claims read: a bounded primary read, a declared whole-window fallback, never a skip (OMN-26433).

The guard's fold keeps a CLAIM live until a later TERMINAL or RELEASE, while its lane has written any row in the last 0.75
hours. The rows that fold needs are therefore the CLAIM, TERMINAL and RELEASE rows of the window and every row of the last
hour; reading every row of 168 hours (84,000 rows) needs many reply pages, and on 2026-11-22 each page of the dev broker took 55 to
137 seconds, so that read could not answer inside any bound. Both reads go through the ledger reader node and none opens the ledger
file. A read that fails or times out is ``LedgerReadError``: the fallback is tried, and when it fails too the caller refuses
unchecked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import yaml

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
    """The claim rows of the window and the heartbeat rows of its last hours, read at once."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = pool.submit(
            reader.rows,
            reader=config.reader,
            window_hours=config.window_hours,
            kinds=config.claim_kinds,
        )
        heartbeat = pool.submit(
            reader.rows,
            reader=config.reader,
            window_hours=config.heartbeat_window_hours,
        )
        claim_rows, heartbeat_rows = claims.result(), heartbeat.result()
    # A recent CLAIM is in both reads; a row listed twice would be a claim counted twice.
    return tuple(dict.fromkeys((*claim_rows, *heartbeat_rows)))


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
