# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox reuse contracts say what their evidence shows at the merge base.

AC1 of the bound-tests contract wants a non-zero exit at the merge base, so its falsifier is an exit of zero
there, under the one selection its evidence command runs. AC2 of the reuse contract is a preservation
criterion: its behavioural tests also pass at the merge base, which sent every replay.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
REUSE_TESTS = Path(__file__).with_name("test_outbox_refused_receipt_reuse.py")
SELECTION = re.compile(r'-k "([^"]+)"')


def _load(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((CONTRACTS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _ac(contract: dict[str, Any], ac_id: str) -> str:
    for requirement in contract["requirements"]:
        for criterion in requirement["acceptance"]:
            if criterion["id"] == ac_id:
                return str(criterion["statement"])
    raise AssertionError(ac_id)


def test_contract_omn_24827_ac1_reads_each_bound_selection_exits_non_zero_at_base() -> (
    None
):
    contract = _load("OMN-24827.yaml")
    statement = _ac(contract, "AC1")
    assert "each bound selection" in statement
    assert "exits non-zero" in statement
    assert "every bound test" not in statement
    assert "-- falsifier:" in statement
    assert statement.rstrip().endswith(
        "exits zero with the merge-base outbox module loaded"
    )


def test_contract_omn_24827_ac1_names_one_selection_everywhere() -> None:
    contract = _load("OMN-24827.yaml")
    selections = {
        m.group(1)
        for text in (
            _ac(contract, "AC1"),
            *(req["command"] for req in contract["evidence_requirements"]),
            *(
                check["check_value"]
                for item in contract["dod_evidence"]
                if "AC1" in item.get("binds_ac", [])
                for check in item["checks"]
            ),
        )
        for m in SELECTION.finditer(text)
    }
    assert selections == {"stored_refusal_lookup or no_new_report"}


def test_contract_omn_24250_ac2_description_says_it_is_a_preservation_criterion() -> (
    None
):
    contract = _load("OMN-24250.yaml")
    (item,) = [i for i in contract["dod_evidence"] if i["id"] == "dod-omn24250-ac2"]
    assert "preservation criterion" in item["description"]


def _dod_check(contract: dict[str, Any], item_id: str) -> str:
    (item,) = [i for i in contract["dod_evidence"] if i["id"] == item_id]
    (check,) = item["checks"]
    return str(check["check_value"])


def test_contract_omn_24875_ac4_selection_covers_every_contract_test() -> None:
    """The AC4 selection names the ticket of each ``test_contract_omn_<n>_`` test here, so none is missed."""
    selection = SELECTION.search(
        _dod_check(_load("OMN-24875.yaml"), "dod-omn24875-ac4")
    )
    assert selection is not None
    tickets = {
        name.split("_")[3]
        for name in globals()
        if name.startswith("test_contract_omn_")
    }
    assert tickets == {"24827", "24250", "24875"}
    for ticket in tickets:
        assert f"omn_{ticket}" in selection.group(1).lower()


def test_contract_omn_24875_ac4_bound_selection_names_tests_in_the_reuse_module() -> (
    None
):
    """Each token of the OMN-24827 bound selection names a test of the reuse module the contracts bind."""
    selection = SELECTION.search(
        _dod_check(_load("OMN-24827.yaml"), "dod-omn24827-ac1")
    )
    assert selection is not None
    names = [
        node.name
        for node in ast.walk(ast.parse(REUSE_TESTS.read_text(encoding="utf-8")))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("test_")
    ]
    for token in selection.group(1).split(" or "):
        assert [name for name in names if token in name], token


async def test_contract_omn_24875_ac4_outbox_under_test_reuses_a_clash_refusal(
    tmp_path: Path,
) -> None:
    """The bound behaviour runs on the outbox module that is loaded: a clash refusal is not sent twice.

    The contract tests above read only contracts/ and a test file, so they pass whatever outbox module is
    loaded. This one drives that module: the merge-base outbox sends every replay, so it sends twice and fails.
    """
    request_id = UUID("3f1c2a9e-5b7d-4c1e-8a2f-6d4e9b0c7a22")
    request = ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-11-02T02:41:00Z | FRICTION | lane=writer-lane | req={request_id} | a row\n",
        requested_by_lane="writer-lane",
        requesting_host="h-test",
        requested_at=datetime(2026, 11, 2, 2, 41, tzinfo=UTC),
    )
    calls: list[int] = []

    async def send(
        queued: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        calls.append(1)
        return ModelWorkLedgerAppendReceipt(
            request_id=queued.request_id,
            status=EnumWorkLedgerAppendStatus.REFUSED,
            exit_code=65,
            message=f"request id {queued.request_id} already belongs to different rows or principal: x",
            ledger_lines=[],
            ledger_host="h-test",
            duration_ms=1,
        )

    outbox.queue_request(tmp_path, request)
    await outbox.drain(tmp_path, send)
    outbox.queue_request(tmp_path, request)
    result = await outbox.drain(tmp_path, send)

    assert len(calls) == 1
    assert result.refused == 1
