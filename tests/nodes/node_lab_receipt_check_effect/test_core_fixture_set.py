# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21826 AC2: the planted fixtures of the omnibase_core lab_receipt set, through the node.

Every fixture is replayed through the recorded GitHub, carrier and lab seams in both phases. The
node's verdict is the core checker's own verdict for that phase (called directly as the oracle),
and in the phase a fixture is written for it is also the committed golden report.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase
from omnibase_core.models.lab_receipt.model_lab_receipt_check_input import (
    ModelLabReceiptCheckInput,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_check_result import (
    ModelLabReceiptCheckResult as CoreCheckResult,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_document import (
    ModelLabReceiptDocument,
)
from omnibase_core.validation.checker_lab_receipt import check_lab_receipt
from omnibase_core.validation.validator_lab_receipt import ValidatorLabReceipt

from omnimarket.nodes.node_lab_receipt_check_effect.handlers.handler_lab_receipt_check import (
    HandlerLabReceiptCheck,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    EnumLabReceiptVerdict,
    ModelLabReceiptCheckResult,
)
from tests.nodes.node_lab_receipt_check_effect.support import (
    FIXTURES,
    InMemoryCarrier,
    InMemoryLab,
    RecordedGithub,
    fixture_names,
    load_document,
    make_request,
)

pytestmark = pytest.mark.unit

GOLDEN = json.loads((FIXTURES / "golden_lab_receipt.json").read_text("utf-8"))
PHASES = (EnumLabReceiptPhase.PRESENT, EnumLabReceiptPhase.VERIFY)


async def run_node(
    document: ModelLabReceiptDocument, phase: EnumLabReceiptPhase
) -> ModelLabReceiptCheckResult:
    facts = document.facts
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(facts.receipts),
        lab_reader=InMemoryLab(facts.lab_reads),
    )
    return await handler.handle(make_request(document, phase))


def oracle(
    document: ModelLabReceiptDocument, phase: EnumLabReceiptPhase
) -> CoreCheckResult:
    return check_lab_receipt(
        ModelLabReceiptCheckInput(
            source_name="7.lab-receipt.json",
            document=document.model_copy(update={"phase": phase}),
            contract=ValidatorLabReceipt().contract,
        )
    )


def test_core_fixture_set_is_the_pinned_copy() -> None:
    sums = (FIXTURES / "SHA256SUMS").read_text("utf-8").splitlines()
    pinned = {line.split()[1]: line.split()[0] for line in sums}
    on_disk = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURES.glob("*.json")
    }
    assert on_disk == pinned
    # 30 planted fixtures in core; counted_internal_source names a private repository and is not copied
    assert len(fixture_names()) == 29


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("name", fixture_names())
async def test_core_fixture_set_node_verdict_is_the_core_verdict(
    name: str, phase: EnumLabReceiptPhase
) -> None:
    document = load_document(name)
    result = await run_node(document, phase)
    expected = oracle(document, phase)
    assert [i.code for i in result.issues] == [i.code for i in expected.issues]
    assert result.required is expected.required
    assert result.classes == expected.classes
    assert result.merge_blocked is (expected.exit_code != 0)
    assert result.phase is phase


@pytest.mark.parametrize("name", fixture_names())
async def test_core_fixture_set_matches_the_golden_report_in_its_own_phase(
    name: str,
) -> None:
    document = load_document(name)
    result = await run_node(document, document.phase)
    assert {
        "issues": [i.code for i in result.issues],
        "required": result.required,
        "classes": list(result.classes),
    } == GOLDEN[name]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(
    "name", [n for n in fixture_names() if n.startswith("violation_")]
)
async def test_core_fixture_set_violation_is_refused_naming_its_rule(
    name: str, phase: EnumLabReceiptPhase
) -> None:
    document = load_document(name)
    rule = name.removeprefix("violation_")
    result = await run_node(document, document.phase)
    assert [i.code for i in result.issues] == [rule]
    assert result.verdict is EnumLabReceiptVerdict.REFUSED
    assert result.merge_blocked is True
    assert rule in result.reason or result.issues[0].message in result.reason


@pytest.mark.parametrize("name", [n for n in fixture_names() if n.startswith("clean_")])
async def test_core_fixture_set_clean_pair_yields_no_issue(name: str) -> None:
    document = load_document(name)
    result = await run_node(document, document.phase)
    assert result.issues == ()
    assert result.merge_blocked is False
    assert result.verdict is EnumLabReceiptVerdict.PASS
