# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read sequence facts and produce a gap report; database failures propagate."""

from dataclasses import asdict, replace
from pathlib import Path

import yaml

from omnimarket.handlers.work_ledger_seq_gap import (
    PostgresWorkLedgerSeqReader,
    build_report,
)
from omnimarket.models.model_work_ledger_seq_gap_report import (
    ModelWorkLedgerSeqGapReport,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect.models import (
    ModelWorkLedgerSeqGapRequest,
)
from omnimarket.protocols.protocol_work_ledger_seq_reader import (
    ProtocolWorkLedgerSeqReader,
)


class HandlerWorkLedgerSeqGap:
    def __init__(self, reader: ProtocolWorkLedgerSeqReader | None = None) -> None:
        if reader is None:
            contract = yaml.safe_load(
                (Path(__file__).parents[1] / "contract.yaml").read_text()
            )
            source = contract["work_ledger_seq_gap"]["ledger_source"]
            reader = PostgresWorkLedgerSeqReader(
                dsn_env=source["dsn_env"], relation=source["relation"]
            )
        self._reader = reader

    def handle(
        self, request: ModelWorkLedgerSeqGapRequest
    ) -> ModelWorkLedgerSeqGapReport:
        facts = self._reader.read_gap_facts(
            ledger_id=request.ledger_id,
            from_seq=request.from_seq,
            since=request.since,
            until=request.until,
        )
        return build_report(
            **asdict(replace(facts, expected_max_seq=request.expected_max_seq))
        )
