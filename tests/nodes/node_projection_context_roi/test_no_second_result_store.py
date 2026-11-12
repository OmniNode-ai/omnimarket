# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: no second result store for code-eval trials (OMN-23628).

Pins that the code-eval trial columns land on the existing
``context_roi_scores`` table rather than a parallel node/table. Red until the
source change (new columns on ``ModelAttemptReductionRow``, the 004 migration,
and the contract exposures) is written.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_CONTRACT = _NODES / "node_projection_context_roi/contract.yaml"
_MIGRATION = (
    _NODES
    / "node_projection_context_roi/migrations/004_add_code_eval_trial_columns.sql"
)

_CODE_EVAL_TABLE = re.compile(r"\bcode_eval_\w*")


def _names_second_store(text: str) -> bool:
    """True when ``text`` declares a table whose name starts with ``code_eval_``."""
    return _CODE_EVAL_TABLE.search(text) is not None


def test_no_second_result_store_directory_exists() -> None:
    """No node directory named ``node_projection_code_eval`` may exist."""
    assert not (_NODES / "node_projection_code_eval").exists()


def test_no_migration_or_contract_names_a_code_eval_table() -> None:
    """No node migration or contract may reference a ``code_eval_*`` table."""
    offenders: list[str] = []
    for path in _NODES.glob("*/migrations/*.sql"):
        if _names_second_store(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    for path in _NODES.glob("*/contract.yaml"):
        if _names_second_store(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == [], f"second result store declared in: {offenders}"


def test_context_roi_scores_is_the_only_contract_naming_that_table() -> None:
    """Only the context_roi_scores node's ``db_io.db_tables`` names the table."""
    owners: list[str] = []
    for path in _NODES.glob("*/contract.yaml"):
        with open(path) as handle:
            contract = yaml.safe_load(handle)
        tables = (contract.get("db_io") or {}).get("db_tables") or []
        if "context_roi_scores" in {table.get("name") for table in tables}:
            owners.append(path.parent.name)
    assert owners == ["node_projection_context_roi"], f"owners={owners}"


def test_positive_control_helper_flags_second_store_and_not_real_migration() -> None:
    """The helper fires on a second store and stays quiet on the real 004."""
    assert _names_second_store("CREATE TABLE code_eval_trials (id int)") is True
    assert _names_second_store(_MIGRATION.read_text(encoding="utf-8")) is False


def test_migration_adds_one_column_per_new_field() -> None:
    """The 004 migration adds every new column to ``context_roi_scores``."""
    text = _MIGRATION.read_text(encoding="utf-8")
    for column in (
        "check_verdict",
        "unscorable_source",
        "row_kind",
        "try_end",
        "suite_digest",
        "size_band",
        "sequence_id",
        "diff_hash",
        "try_ordinal",
        "max_turns",
        "try_timeout_s",
        "generation_s",
        "env_sync_s",
        "check_s",
    ):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {column}\b", text), (
            f"missing column {column}"
        )
    assert "context_roi_scores" in text
    for forbidden in ("DROP", "DELETE", "UPDATE", "TRUNCATE"):
        assert forbidden not in text.upper(), f"forbidden DDL {forbidden}"


def test_contract_exposes_new_columns_and_declares_dlq_topic() -> None:
    """contract.yaml lists the new columns and the projection DLQ topic."""
    with open(_CONTRACT) as handle:
        contract = yaml.safe_load(handle)
    columns = contract["projection_api"]["exposures"][0]["columns"]
    for column in (
        "check_verdict",
        "unscorable_source",
        "row_kind",
        "try_end",
        "suite_digest",
        "size_band",
        "sequence_id",
        "diff_hash",
        "try_ordinal",
        "max_turns",
        "try_timeout_s",
        "generation_s",
        "env_sync_s",
        "check_s",
    ):
        assert column in columns, f"missing exposure column {column}"
    assert contract["event_bus"]["dlq_topics"] == [
        "onex.dlq.omnimarket.projection-context-roi-malformed.v1"
    ]
