# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the latest-state read (OMN-23694): each fails when either side changes shape.

Consumers (their branches read, never guessed):
* the board floor step, lane board-projections-3790, omnimarket#5808 (branch omn-23669-floor-reads-projections):
  its claim, terminal, lane-status reads move from rows pages to one latest-state read per subject kind;
* the work-state projection, lane placement-feeds-3840, omnimarket#5810 (branch omn-23691-work-state-ledger-route):
  its ledger source moves from the row-by-kind command read to the latest-state read.
Each consumer's request and the answer fields it reads are spelled here, in this repository, so the test runs
everywhere and a rename on either side turns it red. The producer seams are the contract, the migration and the writer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel, ValidationError, create_model

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerLatestEntry,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_projection_work_ledger.handlers import (
    handler_work_ledger_projection as writer,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[4] / "src/omnimarket/nodes"
_QUERY_CONTRACT = yaml.safe_load(
    (_ROOT / "node_work_ledger_query_effect/contract.yaml").read_text()
)
_PROJECTION_CONTRACT = yaml.safe_load(
    (_ROOT / "node_projection_work_ledger/contract.yaml").read_text()
)
_MIGRATION = (
    _ROOT / "node_projection_work_ledger/migrations/0011_work_ledger_latest.sql"
).read_text()
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)

# What each consumer sends, as the consumer builds it (the bus read command's JSON), and what it reads back.
BOARD_REQUEST = {
    "query": "latest-state",
    "subject_kind": "lane",
    "kinds": ["CLAIM", "TERMINAL", "STATUS"],
    "since": (NOW - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "page_size": 1000,
}
WORK_STATE_REQUEST = {
    "query": "latest-state",
    "subject_kind": "ticket",
    "kinds": ["CLAIM", "TERMINAL", "STATUS", "MSG", "HOLD"],
    "since": (NOW - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "page_size": 1000,
}
ANSWER_FIELDS_READ = (
    "status",
    "error",
    "latest",
    "next_cursor",
    "watermark",
    "page_size",
)
ENTRY_FIELDS_READ = (
    "subject_kind",
    "subject",
    "row_type",
    "row_id",
    "at",
    "lane",
    "ticket",
    "pr",
    "outcome",
    "to",
)


def missing_fields(model: type[BaseModel], wanted: tuple[str, ...]) -> list[str]:
    """The fields a consumer reads that the model no longer has."""
    return [name for name in wanted if name not in model.model_fields]


@pytest.mark.parametrize("request_fields", [BOARD_REQUEST, WORK_STATE_REQUEST])
def test_seam_the_request_each_consumer_sends_is_a_valid_latest_state_request(
    request_fields: dict[str, object],
) -> None:
    request = ModelWorkLedgerQueryRequest.model_validate(
        {**request_fields, "correlation_id": str(uuid4())}
    )
    assert request.query.value == "latest-state"
    assert request.kinds == tuple(cast(list[str], request_fields["kinds"]))


def test_seam_a_request_field_a_consumer_sends_renamed_in_the_model_is_refused() -> (
    None
):
    """Failing control: the request model refuses an unknown field, so a rename on the node side turns the seam red."""
    with pytest.raises(ValidationError):
        ModelWorkLedgerQueryRequest.model_validate(
            {**BOARD_REQUEST, "subject_type": "lane", "correlation_id": str(uuid4())}
        )


def test_seam_the_answer_fields_the_consumers_read_exist() -> None:
    assert missing_fields(ModelWorkLedgerQueryResult, ANSWER_FIELDS_READ) == []
    assert missing_fields(ModelWorkLedgerLatestEntry, ENTRY_FIELDS_READ) == []


def test_seam_a_planted_rename_of_an_answer_field_is_found() -> None:
    fields: dict[str, Any] = {
        n: (str | None, None) for n in ENTRY_FIELDS_READ if n != "subject"
    }
    renamed = create_model("Renamed", subject_name=(str, ...), **fields)
    assert missing_fields(renamed, ENTRY_FIELDS_READ) == ["subject"]


def test_seam_the_answer_survives_the_bus_as_json_unchanged() -> None:
    entry = ModelWorkLedgerLatestEntry(
        subject_kind="lane",
        subject="alpha",
        row_type="TERMINAL",
        row_id="r1",
        at=NOW,
        lane="alpha",
        outcome="done",
    )
    result = ModelWorkLedgerQueryResult(
        correlation_id=uuid4(),
        query="latest-state",
        window_since=None,
        window_until=NOW,
        latest=(entry,),
        watermark=NOW,
        page_size=1000,
    )
    assert (
        ModelWorkLedgerQueryResult.model_validate_json(result.model_dump_json())
        == result
    )


def test_seam_the_contract_and_the_migration_track_the_same_row_types() -> None:
    declared = set(_PROJECTION_CONTRACT["work_ledger"]["latest_row_types"])
    listed = re.search(r"row_type IN \(([^)]*)\)", _MIGRATION)
    assert listed is not None
    assert set(re.findall(r"'([A-Z-]+)'", listed.group(1))) == declared


def test_seam_the_failing_control_a_row_type_added_to_one_side_only_is_found() -> None:
    declared = set(_PROJECTION_CONTRACT["work_ledger"]["latest_row_types"]) | {"RULING"}
    listed = re.search(r"row_type IN \(([^)]*)\)", _MIGRATION)
    assert listed is not None
    assert set(re.findall(r"'([A-Z-]+)'", listed.group(1))) != declared


def test_seam_the_query_node_reads_the_table_the_migration_creates_and_the_projection_writes() -> (
    None
):
    created = re.search(
        r"CREATE TABLE IF NOT EXISTS (omninode_internal\.work_ledger_latest) \(",
        _MIGRATION,
    )
    assert created is not None
    assert _QUERY_CONTRACT["work_ledger_query"]["ledger_source"][
        "latest_relation"
    ] == created.group(1)
    assert created.group(1) == writer.LATEST_TABLE
    for contract in (_QUERY_CONTRACT, _PROJECTION_CONTRACT):
        tables = {t["name"]: t for t in contract["db_io"]["db_tables"]}
        assert (
            tables["work_ledger_latest"]["migration"] == "0011_work_ledger_latest.sql"
        )


def test_seam_the_writer_columns_are_the_tables_columns() -> None:
    table = re.search(
        r"CREATE TABLE IF NOT EXISTS omninode_internal\.work_ledger_latest \((.*?)\n\);",
        _MIGRATION,
        re.S,
    )
    assert table is not None
    columns = [
        line.split()[0]
        for line in table.group(1).splitlines()
        if line.strip() and not line.strip().startswith(("PRIMARY", "CONSTRAINT", "--"))
    ]
    insert = re.search(
        r"INSERT INTO \{LATEST_TABLE\}\s*\((.*?)\)",
        Path(writer.__file__).read_text(),
        re.S,
    )
    assert insert is not None
    written = [c.strip() for c in insert.group(1).split(",")]
    assert written == columns
