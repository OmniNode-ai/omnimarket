# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a generated chain scenario test module calls (OMN-19998).

A generated module is thin: it loads the node's contract and parametrizes over the
cases the contract declares. Running a case, proving that a named break turns it red
and keeping the receipts of that proof live here, so one reviewed implementation
serves every node.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnibase_core.enums.enum_chain_scenario_break import EnumChainScenarioBreak
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_case import (
    ModelChainScenarioCase,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_receipt import (
    ModelChainScenarioReceipt,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_receipt_set import (
    ModelChainScenarioReceiptSet,
)

from omnimarket.handlers.chain_scenarios.assertions import assert_scenario
from omnimarket.handlers.chain_scenarios.errors import ChainScenarioAssertionError
from omnimarket.handlers.chain_scenarios.model_chain_scenario_overlay import (
    load_overlay,
)
from omnimarket.handlers.chain_scenarios.runner import ChainScenarioNode

RECEIPTS_FILE = "chain_scenario_receipts.json"
WRITE_ENV = "CHAIN_GEN_WRITE"


def load_node(contract_path: Path) -> ChainScenarioNode:
    """The node under test, with the overlay the environment selects."""
    return ChainScenarioNode(contract_path, load_overlay())


async def assert_case(node: ChainScenarioNode, case: ModelChainScenarioCase) -> None:
    """Run the case unbroken and assert it."""
    run = await node.run(case)
    assert_scenario(run, case, node.scenario)


def break_pairs(
    node: ChainScenarioNode,
) -> list[tuple[ModelChainScenarioCase, EnumChainScenarioBreak]]:
    """Every (case, break) pair the contract says the case must survive."""
    return [(case, b) for case in node.scenario.cases for b in case.breaks]


async def prove_break(
    node: ChainScenarioNode,
    case: ModelChainScenarioCase,
    injected_break: EnumChainScenarioBreak,
) -> ModelChainScenarioReceipt:
    """Run the case under the break; red only if an assertion of the case fired.

    The unbroken case must pass first: a case that is already red proves nothing about
    the break, and an error other than a failed assertion is not a kill.
    """
    await assert_case(node, case)
    run = await node.run(case, injected_break)
    try:
        assert_scenario(run, case, node.scenario)
    except ChainScenarioAssertionError as exc:
        return ModelChainScenarioReceipt(
            case_name=case.case_name,
            case_hash=case.case_hash(),
            injected_break=injected_break,
            outcome="red",
            failed_assertion=exc.code.value,
        )
    return ModelChainScenarioReceipt(
        case_name=case.case_name,
        case_hash=case.case_hash(),
        injected_break=injected_break,
        outcome="green",
    )


async def fresh_receipts(node: ChainScenarioNode) -> ModelChainScenarioReceiptSet:
    """Receipts of a fresh proof of every declared (case, break) pair."""
    return ModelChainScenarioReceiptSet(
        schema_version=1,
        node=node.name,
        receipts=tuple(
            [await prove_break(node, case, b) for case, b in break_pairs(node)]
        ),
    )


def render_receipts(receipts: ModelChainScenarioReceiptSet) -> str:
    return json.dumps(receipts.model_dump(mode="json"), indent=2) + "\n"


def keep_or_compare_receipts(
    node: ChainScenarioNode, fresh: ModelChainScenarioReceiptSet
) -> None:
    """Write the receipts when asked to regenerate; otherwise they must equal the fresh proof."""
    path = node.contract_path.parent / RECEIPTS_FILE
    if os.environ.get(WRITE_ENV) == "1":
        path.write_text(render_receipts(fresh), encoding="utf-8")
    kept = ModelChainScenarioReceiptSet.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    assert kept == fresh, (
        f"{path.name} is not what a fresh proof gives; regenerate with {WRITE_ENV}=1"
    )


__all__ = [
    "RECEIPTS_FILE",
    "WRITE_ENV",
    "assert_case",
    "break_pairs",
    "fresh_receipts",
    "keep_or_compare_receipts",
    "load_node",
    "prove_break",
    "render_receipts",
]
