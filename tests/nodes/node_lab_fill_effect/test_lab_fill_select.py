# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill select step: the handler over the bus ledger reader (OMN-19998).

The select step reads the rolling ledger's window from the bus reader, then runs the selection on
those rows; a not-ok read (stale, unreadable or not-moved) is a typed result with no selection, and
the selection never sees a ledger line the reader did not give it.
"""

from __future__ import annotations

import importlib
import importlib.resources
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_select import (
    ModelLabFillSelectRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.protocol_lab_fill_ledger_rows import (
    LabFillLedgerRows,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lab_fill_selection import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-10T03:00:00Z"  # equals SEAM_NOW of the reader seam tests
HANDOFF_ROW = (
    "2026-11-09T10:25:00Z | MSG | from=fix-235 | to=landing-controller"
    " | ticket=OMN-20677 | pr=repo_a#235"
)


def selection_json() -> dict[str, Any]:
    candidates = (
        ModelLabFillCandidateInput(
            key="OMN-20677",
            kind="pr-red",
            ticket="OMN-20677",
            pr="repo_a#235",
            repo="repo_a",
            pr_heads=("repo_a#235@abc",),
            main_sha="m",
            watcher_read=True,
            facts_read_at=NOW,
            ticket_updated_at="2026-11-10T02:00:00Z",
        ),
        ModelLabFillCandidateInput(
            key="OMN-9999",
            kind="ticket",
            ticket="OMN-9999",
            watcher_read=True,
            facts_read_at=NOW,
            ticket_updated_at="2026-11-10T02:00:00Z",
        ),
    )
    request = ModelLabFillSelectionRequest(
        now=NOW,
        candidates=candidates,
        ledger_lines=(),
        deployment=ModelLabFillDeployment(
            ("change_control",), ("registry",), ("docs", "docs_private")
        ),
    )
    return request.model_dump(mode="json", exclude={"ledger_lines"})


def select_request(**overrides: Any) -> ModelLabFillSelectRequest:
    base: dict[str, Any] = {
        "selection": selection_json(),
        "ledger_reader": "lab-fill-selection",
        "ledger_window_hours": 27,
    }
    base.update(overrides)
    return ModelLabFillSelectRequest.model_validate(base)


@dataclass
class FakeLedgerRows:
    """The port the select step reads its ledger through, with a call log."""

    status: str = "ok"
    lines: tuple[str, ...] = ()
    reason: str = ""
    watermark_age_s: int | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)

    def read_rows(self, reader: str, window_hours: int) -> LabFillLedgerRows:
        self.calls.append((reader, window_hours))
        return LabFillLedgerRows(
            status=self.status,
            lines=self.lines,
            reason=self.reason,
            watermark_age_s=self.watermark_age_s,
        )


class SpySelector(HandlerLabFillSelection):
    """Counts the selection's handle calls so a not-ok read is proven to skip it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def handle(self, request: Any) -> Any:
        self.calls += 1
        return super().handle(request)


def _decisions(result: Any) -> dict[str, tuple[str | None, str]]:
    return {
        decision.ticket: (decision.reason, decision.caller_reason)
        for decision in result.selection.decisions
    }


def test_ok_rows_with_the_handoff_row_select_the_measured_decisions() -> None:
    rows = FakeLedgerRows(lines=(HANDOFF_ROW,))
    result = HandlerLabFillSelect(rows).handle(select_request())
    assert result.ledger_status == "ok"
    assert result.ledger_rows == 1
    assert rows.calls == [("lab-fill-selection", 27)]
    assert _decisions(result) == {
        "OMN-20677": (EnumLabFillSkipReason.HANDED_OFF, "owned:handed-off:repo_a#235"),
        "OMN-9999": (None, ""),
    }


def test_without_the_ledger_rows_the_handed_off_ticket_is_eligible() -> None:
    rows = FakeLedgerRows(lines=())
    result = HandlerLabFillSelect(rows).handle(select_request())
    assert result.ledger_status == "ok"
    assert result.ledger_rows == 0
    assert _decisions(result)["OMN-20677"] == (None, "")


@pytest.mark.parametrize("status", ["stale", "unreadable", "not-moved"])
@pytest.mark.parametrize("reason", ["", "the reason"])
def test_a_not_ok_read_is_a_typed_result_and_never_runs_the_selection(
    status: str, reason: str
) -> None:
    rows = FakeLedgerRows(status=status, reason=reason, lines=(HANDOFF_ROW,))
    selector = SpySelector()
    result = HandlerLabFillSelect(rows, selector=selector).handle(select_request())
    assert result.ledger_status == status
    assert result.ledger_reason == reason
    assert result.selection is None
    assert selector.calls == 0


def test_a_selection_carrying_ledger_lines_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ledger_lines"):
        select_request(selection={**selection_json(), "ledger_lines": (HANDOFF_ROW,)})


def test_a_reader_name_outside_the_pattern_is_rejected() -> None:
    with pytest.raises(ValidationError):
        select_request(ledger_reader="Bad Name")


def test_a_zero_window_is_rejected() -> None:
    with pytest.raises(ValidationError):
        select_request(ledger_window_hours=0)


def test_a_selection_that_does_not_validate_is_rejected_on_an_ok_read() -> None:
    rows = FakeLedgerRows(lines=(HANDOFF_ROW,))
    with pytest.raises(ValidationError):
        HandlerLabFillSelect(rows).handle(select_request(selection={}))


def test_the_operation_is_bound_in_the_contract() -> None:
    package = importlib.import_module("omnimarket.nodes.node_lab_fill_effect")
    contract = yaml.safe_load(
        importlib.resources.files(package).joinpath("contract.yaml").read_text()
    )
    operation = next(
        handler
        for handler in contract["handler_routing"]["handlers"]
        if handler["operation"] == "select_lab_fill_candidates"
    )
    handler = importlib.import_module(operation["handler"]["module"])
    assert operation["handler"]["name"] == "HandlerLabFillSelect"
    assert getattr(handler, operation["handler"]["name"]) is HandlerLabFillSelect
    assert operation["input_model"].endswith("ModelLabFillSelectRequest")
    assert operation["output_model"].endswith("ModelLabFillSelectResult")
