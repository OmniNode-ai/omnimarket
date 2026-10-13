# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden contract chain from the gap request to a shared report."""

from __future__ import annotations

import importlib
import tomllib
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.model_work_ledger_seq_gap_report import WorkLedgerSeqFacts
from omnimarket.nodes.node_work_ledger_seq_gap_effect import (
    HandlerWorkLedgerSeqGap,
    NodeWorkLedgerSeqGapEffect,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect.models import (
    ModelWorkLedgerSeqGapRequest,
)

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[4]
_NODE = _ROOT / "src/omnimarket/nodes/node_work_ledger_seq_gap_effect"


class _Reader:
    def read_gap_facts(
        self,
        *,
        ledger_id: str,
        from_seq: int,
        since: datetime | None,
        until: datetime | None,
    ) -> WorkLedgerSeqFacts:
        return WorkLedgerSeqFacts(
            ledger_id=ledger_id,
            from_seq=from_seq,
            gaps=(),
            duplicates=(),
            duplicate_count=0,
            rows_with_seq=3,
            rows_without_seq=0,
            min_seq=from_seq,
            max_seq=from_seq + 2,
            max_seq_row_ts=None,
            newest_row_ts=None,
            window_since=since,
            window_until=until,
        )


def test_contract_models_handler_metadata_and_entry_point() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    metadata = yaml.safe_load((_NODE / "metadata.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
    for key in ("input_model", "output_model"):
        module, name = contract[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module), name)
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerWorkLedgerSeqGap
    )
    source = contract["work_ledger_seq_gap"]["ledger_source"]
    branch = yaml.safe_load(
        (_NODE.parent / "node_branch_claim_check_effect/contract.yaml").read_text()
    )
    assert source["dsn_env"] == branch["branch_claim"]["ledger_source"]["dsn_env"]
    assert metadata["capabilities"]["side_effect_class"] == "read_only"
    entries = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        entries["node_work_ledger_seq_gap_effect"]
        == "omnimarket.nodes.node_work_ledger_seq_gap_effect"
    )
    assert (
        NodeWorkLedgerSeqGapEffect(_Reader())
        .handle(ModelWorkLedgerSeqGapRequest(correlation_id=uuid4()))
        .exact
    )
    assert HandlerWorkLedgerSeqGap()._reader._dsn_env == source["dsn_env"]
