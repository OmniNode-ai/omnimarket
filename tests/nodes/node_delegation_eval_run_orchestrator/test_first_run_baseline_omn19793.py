# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19793: the eval run reproduces the committed first-run baseline.

tests/fixtures/delegation_eval/omn19793_first_run_verdicts.json holds the content-free verdicts of the EV.4 first run (manifest
4960f691…a5c6, blind opus labels under ev4-blind-v1), as the dev lane stored
them. Run through the eval run handler, they give back the same label-set
digest and eval run id, and the recorded-verdict arm gives back, class by class,
the false-pass and false-refusal counts that
reports/2026-09-30-delegation-gate-false-pass-first-run.md publishes (AC1).
Written through the rule-7a writer, the same run redelivered writes the same
rows and no more (AC2), and the writer never reports zero rows for it (AC3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from omnimarket.events.delegation_eval import ModelDelegationEvalRunRequest
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.protocols.protocol_delegation_eval_labelled_items import (
    ProtocolDelegationEvalLabelledItems,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from omnimarket.nodes.node_projection_delegation_eval.handlers import (
    DelegationEvalProjectionWriter,
)
from tests.test_golden_chain_projection_delegation_eval import _RunStore

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/delegation_eval/omn19793_first_run_verdicts.json"
)
_FIRST_RUN_ID = UUID("735b18b8-734e-5c7d-bc00-47926ac70963")

# The report's per-class table: accepted n and false passes, recorded-verdict arm.
_REPORT_FALSE_PASSES = {
    "code_review": (20, 16),
    "document": (20, 10),
    "planning": (20, 10),
    "research": (20, 7),
    "review": (20, 7),
    "reasoning": (19, 4),
    "summarization": (20, 3),
    "code_generation": (19, 2),
    "test": (20, 1),
}
# "Of 90 refused answers, 25 (27.8%) were labelled adequate."
_REPORT_REFUSED, _REPORT_FALSE_REFUSALS = 90, 25


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data


def _payload(at: datetime = datetime(2026, 9, 30, tzinfo=UTC)) -> Any:
    data = _fixture()
    items = tuple(
        ModelGateEvalItem(
            item_id=row["item_key"],
            task_class=row["task_class"],
            stratum=row["stratum"],
            label=row["label"],
            prompt_text="",
            recorded_verdict=row["recorded_verdict"],
            recorded_deciding_check=row["recorded_deciding_check"],
        )
        for row in data["items"]
    )
    source = Mock(spec=ProtocolDelegationEvalLabelledItems)
    source.get_labelled_items.return_value = items
    # The first run predates the class rubrics, so it ran with none.
    no_rubrics = SimpleNamespace(
        rubric_version="none",
        classes={},
        false_pass_lines={},
        false_refusal_lines={},
    )
    handler = HandlerDelegationEvalRun(
        source,
        HandlerDelegationGateEval(
            Mock(return_value=ModelGateReplayVerdict(verdict="accepted"))
        ),
        clock=lambda: at,
        class_rubrics=lambda: no_rubrics,
    )
    run = data["run"]
    return handler.handle(
        ModelDelegationEvalRunRequest(
            tenant_id="820272f9-4aaf-5add-a2df-0af942852ab2",
            manifest_id=run["manifest_id"],
            rater_role=run["rater_role"],
            rubric_version=run["rubric_version"],
            gate_version=run["gate_version"],
        )
    ).payload


def test_first_run_baseline_reproduces_the_label_set_and_run_id() -> None:
    payload = _payload()
    assert payload.status == "completed"
    assert payload.label_set_sha256 == _fixture()["run"]["label_set_sha256"]
    assert payload.eval_run_id == _FIRST_RUN_ID


def test_first_run_baseline_recorded_arm_matches_the_report_per_class() -> None:
    rows = [
        row
        for row in _payload().results
        if row.arm == "recorded" and row.stratum == "all"
    ]
    false_passes = {
        row.task_class: (row.accepted_n, row.false_pass_count) for row in rows
    }
    assert false_passes == _REPORT_FALSE_PASSES
    assert sum(fp for _, fp in false_passes.values()) == 60
    assert sum(n for n, _ in false_passes.values()) == 178
    assert sum(row.refused_n for row in rows) == _REPORT_REFUSED
    assert sum(row.false_refusal_count for row in rows) == _REPORT_FALSE_REFUSALS


def _writer() -> tuple[DelegationEvalProjectionWriter, _RunStore]:
    writer = DelegationEvalProjectionWriter()
    store = _RunStore()
    writer.__dict__["_db"] = store
    return writer, store


def test_first_run_baseline_redelivered_run_writes_the_same_rows_and_no_more() -> None:
    writer, store = _writer()
    first = _payload().model_dump(mode="json")
    writer.handle(first)
    tables = {t: dict(rows) for t, rows in store.tables.items()}
    assert len(tables["delegation_eval_item_verdicts"]) == len(_fixture()["items"])
    assert len(tables["delegation_eval_results"]) == len(first["results"])
    redelivered = _payload(at=datetime(2026, 9, 30, 0, 5, tzinfo=UTC)).model_dump(
        mode="json"
    )
    assert redelivered["eval_run_id"] == first["eval_run_id"]
    writer.handle(redelivered)
    assert {t: set(rows) for t, rows in store.tables.items()} == {
        t: set(rows) for t, rows in tables.items()
    }
    for table, rows in store.tables.items():
        for key, row in rows.items():
            kept = {k: v for k, v in row.items() if k != "observed_at"}
            assert kept == {
                k: v for k, v in tables[table][key].items() if k != "observed_at"
            }


def test_first_run_baseline_run_completed_row_count_is_never_zero() -> None:
    writer, _ = _writer()
    payload = _payload().model_dump(mode="json")
    first = writer.handle(payload)
    assert first["rows_upserted"] == first["verdict_rows"] + first["result_rows"] > 0
    duplicate = writer.handle(payload)
    assert duplicate["rows_upserted"] == 0
    assert duplicate["rows_refused_by_ordering_guard"] == first["rows_upserted"]
