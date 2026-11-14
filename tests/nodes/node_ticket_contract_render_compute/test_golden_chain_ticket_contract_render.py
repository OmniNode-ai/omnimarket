# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23759: the standard ticket contract renders into the ticket body and parses back.

Golden chain: render, parse, round trip and the create gate's own checker over every fixture. Error
chain: each refusal and finding by code, each with the edit that raises it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.validation import validator_base
from omnibase_core.validation.checker_ticket_creation import check_ticket_creation

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_ticket_contract_render_compute import (
    NodeTicketContractRenderCompute,
)
from omnimarket.nodes.node_ticket_contract_render_compute.handlers.handler_ticket_contract_render import (
    BLOCK_INFO_STRING,
    HandlerTicketContractRender,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderFinding as Finding,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderOperation as Operation,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderOutcome as Outcome,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    ModelTicketContractRenderNormalization,
    ModelTicketContractRenderRequest,
    ModelTicketContractRenderResult,
)
from tests.nodes.node_ticket_contract_render_compute.contract_fixtures import (
    IN_PROGRESS,
    fixture_contracts,
)

pytestmark = pytest.mark.unit

LANE = "fixture-lane-0001"
FILING = (f"created-by-lane: {LANE}", "correlation: 4f1c")
FIXTURES = fixture_contracts()
GATE_PATH = (
    Path(validator_base.__file__).parent
    / "contracts"
    / "ticket_creation.validation.yaml"
)


def _gate() -> ModelValidatorSubcontract:
    loaded = yaml.safe_load(GATE_PATH.read_text("utf-8"))["validation"]
    return ModelValidatorSubcontract.model_validate(loaded)


def _render(
    contract: ModelTicketContract, filing: tuple[str, ...] = FILING
) -> ModelTicketContractRenderResult:
    return HandlerTicketContractRender().handle(
        ModelTicketContractRenderRequest(
            operation=Operation.RENDER, contract=contract, filing_lines=filing
        )
    )


def _body(name: str, filing: tuple[str, ...] = FILING) -> str:
    result = _render(FIXTURES[name], filing)
    assert result.outcome is Outcome.RENDERED, result.findings
    return result.body


def _edit_above_block(body: str, old: str, new: str) -> str:
    """The body with ``old`` replaced only in the text above the contract block."""
    fence = "```" + BLOCK_INFO_STRING
    head, _, block = body.partition(fence)
    assert old in head
    return head.replace(old, new) + fence + block


def _edit_block(body: str, old: str, new: str) -> str:
    """The body with ``old`` replaced only inside the contract block."""
    fence = "```" + BLOCK_INFO_STRING
    head, _, block = body.partition(fence)
    assert old in block
    return head + fence + block.replace(old, new)


def _parse(
    body: str, normalizations: tuple[ModelTicketContractRenderNormalization, ...] = ()
) -> ModelTicketContractRenderResult:
    return HandlerTicketContractRender().handle(
        ModelTicketContractRenderRequest(
            operation=Operation.PARSE, body=body, normalizations=normalizations
        )
    )


def _codes(result: ModelTicketContractRenderResult) -> set[str]:
    return {finding.code.value for finding in result.findings}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_render_passes_create_gate(name: str) -> None:
    contract = FIXTURES[name]
    payload = {
        "title": contract.title,
        "description": _body(name),
        "parentId": "00000000-0000-0000-0000-000000000001",
        "labels": ["lane-created"],
        "state": "In Progress" if name in IN_PROGRESS else "Backlog",
    }
    verdict = check_ticket_creation(payload, _gate(), lane=LANE)
    assert verdict.findings == (), [f"{f.code}: {f.reason}" for f in verdict.findings]


def test_render_passes_create_gate_positive_control() -> None:
    """The gate is the real one: a body with its Gate line removed is refused."""
    body = _body("code_only")
    without_gate = "\n".join(
        line for line in body.split("\n") if not line.startswith("Gate:")
    )
    payload = {
        "title": "t",
        "description": without_gate,
        "parentId": "00000000-0000-0000-0000-000000000001",
        "labels": ["lane-created"],
    }
    assert (
        "missing_gate_line" in check_ticket_creation(payload, _gate(), lane=LANE).codes
    )


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_round_trip(name: str) -> None:
    contract = FIXTURES[name]
    parsed = _parse(_body(name))
    assert parsed.outcome is Outcome.PARSED
    assert parsed.findings == ()
    assert parsed.contract is not None
    assert parsed.contract.compute_fingerprint() == contract.compute_fingerprint()
    assert parsed.block_fingerprint == contract.contract_fingerprint
    assert parsed.recomputed_fingerprint == contract.compute_fingerprint()
    assert parsed.filing_lines == FILING


def test_round_trip_render_is_deterministic() -> None:
    assert _body("deployed") == _body("deployed")


def test_text_drift() -> None:
    body = _body("deployed")
    edited = _edit_above_block(
        body, "the unit holds for every fixture", "the unit holds sometimes"
    )
    parsed = _parse(edited)
    assert _codes(parsed) == {Finding.RENDERED_TEXT_DRIFT.value}


def test_text_drift_on_removed_gate_line() -> None:
    body = _body("code_only")
    edited = "\n".join(
        line for line in body.split("\n") if not line.startswith("Gate:")
    )
    assert _codes(_parse(edited)) == {Finding.RENDERED_TEXT_DRIFT.value}


def test_text_drift_on_text_after_the_block() -> None:
    parsed = _parse(_body("code_only") + "\nA note added below the block.\n")
    assert _codes(parsed) == {Finding.RENDERED_TEXT_DRIFT.value}


def test_block_edited() -> None:
    body = _body("code_only")
    # Only the block copy changes; the text above it is the original.
    edited = _edit_block(
        body, "the unit holds for every fixture", "the unit holds for no fixture"
    )
    parsed = _parse(edited)
    assert Finding.CONTRACT_NOT_SYNCED.value in _codes(parsed)
    assert parsed.block_fingerprint != parsed.recomputed_fingerprint


def test_block_edited_with_fingerprint_restamped_is_not_caught_by_its_own_hash() -> (
    None
):
    """Rule 3: a block changed and restamped recomputes equal; only the text above shows it."""
    contract = FIXTURES["code_only"]
    changed = contract.model_copy(deep=True)
    object.__setattr__(changed, "summary", "changed outside the node")
    changed.contract_fingerprint = changed.compute_fingerprint()
    body = _body("code_only")
    head = body.split("```" + BLOCK_INFO_STRING)[0]
    restamped = head + "```" + BLOCK_INFO_STRING + "\n" + changed.to_yaml() + "```\n"
    parsed = _parse(restamped)
    assert Finding.CONTRACT_NOT_SYNCED.value not in _codes(parsed)


def test_filing_lines_not_compared() -> None:
    other = ("created-by-lane: some-other-lane-9999", "correlation: ffff", "extra line")
    parsed_other = _parse(_body("deployed", other))
    parsed_same = _parse(_body("deployed", FILING))
    assert parsed_other.findings == ()
    assert Finding.RENDERED_TEXT_DRIFT.value not in _codes(parsed_other)
    assert parsed_other.filing_lines == other
    assert parsed_other.contract is not None
    assert parsed_same.contract is not None
    assert (
        parsed_other.contract.compute_fingerprint()
        == parsed_same.contract.compute_fingerprint()
    )


def test_filing_lines_not_compared_when_there_are_none() -> None:
    parsed = _parse(_body("code_only", ()))
    assert parsed.findings == ()
    assert parsed.filing_lines == ()


def test_render_refuses_a_stale_fingerprint() -> None:
    stale = FIXTURES["code_only"].model_copy(deep=True)
    stale.contract_fingerprint = "0000000000000000"
    result = _render(stale)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_NOT_SYNCED.value}


def test_render_refuses_filing_lines_that_would_read_as_the_brief_heading() -> None:
    result = _render(FIXTURES["code_only"], ("created-by-lane: x", "## In plain terms"))
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.RENDERED_TEXT_DRIFT.value}


def test_parse_refuses_a_body_without_the_block() -> None:
    body = _body("code_only").split("```" + BLOCK_INFO_STRING)[0]
    result = _parse(body)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_BLOCK_MISSING.value}


def test_parse_refuses_two_blocks() -> None:
    body = _body("code_only")
    result = _parse(body + "\n" + body)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_BLOCK_DUPLICATED.value}


def test_parse_refuses_a_block_that_is_not_closed() -> None:
    body = _body("code_only").rstrip("\n")
    assert body.endswith("```")
    result = _parse(body[: -len("```")])
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_BLOCK_UNPARSEABLE.value}


def test_parse_refuses_a_block_that_is_not_yaml() -> None:
    body = f"```{BLOCK_INFO_STRING}\n: : [unclosed\n```\n"
    result = _parse(body)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_BLOCK_UNPARSEABLE.value}


def test_parse_refuses_a_block_with_a_duplicate_key() -> None:
    body = _body("code_only")
    doubled = body.replace("title: ", "title: first\ntitle: ", 1)
    result = _parse(doubled)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_BLOCK_UNPARSEABLE.value}


def test_parse_refuses_a_block_the_model_refuses() -> None:
    body = _body("code_only").replace("business_brief:", "business_brief_gone:", 1)
    result = _parse(body)
    assert result.outcome is Outcome.REFUSED
    assert _codes(result) == {Finding.CONTRACT_INVALID.value}


def test_normalizations_forgive_only_what_the_overlay_names() -> None:
    body = _body("code_only")
    rewrapped = body.replace("Gate: ", "Gate:  ", 1)
    assert _codes(_parse(rewrapped)) == {Finding.RENDERED_TEXT_DRIFT.value}
    forgiven = _parse(
        rewrapped,
        (
            ModelTicketContractRenderNormalization(
                pattern=r"Gate:  ", replacement="Gate: "
            ),
        ),
    )
    assert forgiven.findings == ()


def test_normalization_pattern_must_compile() -> None:
    with pytest.raises(ValueError, match="not a regular expression"):
        ModelTicketContractRenderNormalization(pattern="(")


def test_a_request_carries_its_input() -> None:
    with pytest.raises(ValueError, match="carries a contract"):
        ModelTicketContractRenderRequest(operation=Operation.RENDER)
    with pytest.raises(ValueError, match="carries a body"):
        ModelTicketContractRenderRequest(operation=Operation.PARSE, body="  ")


def test_the_block_is_only_the_contracts_yaml() -> None:
    contract = FIXTURES["deployed"]
    body = _body("deployed")
    assert body.rstrip("\n").endswith("```")
    inner = body.split("```" + BLOCK_INFO_STRING + "\n")[1].rsplit("```", 1)[0]
    assert inner == contract.to_yaml()


def test_a_fence_in_the_contract_text_gets_a_longer_fence() -> None:
    body = _body("fenced")
    assert f"````{BLOCK_INFO_STRING}\n" not in body or "`````" in body
    parsed = _parse(body)
    assert parsed.findings == ()
    assert parsed.contract is not None
    assert parsed.contract.summary == FIXTURES["fenced"].summary


def test_integrated_criterion_is_marked_and_probe_and_diagnosis_lines_render() -> None:
    assert "- [ ] AC2: integrated: done only when" in _body("deployed")
    probed = _body("probed")
    assert "Probe: git log -1 => the branch has a commit" in probed
    assert "Diagnosis: the earlier node refused every body" in probed
    assert "Gate: OMN-90000 AC-1" in probed
    assert "Gate: OMN-90000 AC-3" in _body("deployed")
    assert "\n## Contract\n\n```yaml onex-ticket-contract\n" in _body("code_only")


def test_golden_chain_event_over_the_declared_topics() -> None:
    """A render request on the declared command topic is answered on the declared terminal topic."""
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    node_contract = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_ticket_contract_render_compute/contract.yaml"
    )

    class _AsyncHandler:
        async def handle(
            self, request: ModelTicketContractRenderRequest
        ) -> ModelTicketContractRenderResult:
            return NodeTicketContractRenderCompute().handle(request)

    async def run(request: ModelTicketContractRenderRequest) -> dict[str, Any]:
        bus = EventBusInmemory(environment="render-proof", group="render-proof")
        await bus.start()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        command = contract_subscribe_topics(node_contract)[0]
        terminal = contract_publish_topics(node_contract)[0]
        assert command == "onex.cmd.omnimarket.ticket-contract-render.v1"
        assert terminal == "onex.evt.omnimarket.ticket-contract-rendered.v1"

        async def observe(message: Any) -> None:
            await queue.put(json.loads(message.value))

        adapter = LocalRuntimeBusAdapter(
            handler=_AsyncHandler(),
            handler_name="HandlerTicketContractRender",
            input_model_cls=ModelTicketContractRenderRequest,
            output_topic=terminal,
            bus=bus,
            on_error=lambda: None,
        )
        stops = [
            await bus.subscribe_ungrouped(terminal, observe),
            await bus.subscribe_ungrouped(command, adapter.on_message),
        ]
        await bus.publish(command, None, request.model_dump_json().encode())
        answer = await asyncio.wait_for(queue.get(), timeout=10)
        for stop in stops:
            await stop()
        await bus.close()
        return answer

    contract = FIXTURES["deployed"]
    rendered = asyncio.run(
        run(
            ModelTicketContractRenderRequest(
                operation=Operation.RENDER, contract=contract, filing_lines=FILING
            )
        )
    )
    assert rendered["outcome"] == "rendered"
    assert rendered["body"] == _body("deployed")
    assert rendered["recomputed_fingerprint"] == contract.compute_fingerprint()

    parsed = asyncio.run(
        run(
            ModelTicketContractRenderRequest(
                operation=Operation.PARSE, body=rendered["body"]
            )
        )
    )
    assert parsed["outcome"] == "parsed"
    assert parsed["findings"] == []
    assert parsed["block_fingerprint"] == contract.contract_fingerprint

    drifted = asyncio.run(
        run(
            ModelTicketContractRenderRequest(
                operation=Operation.PARSE,
                body=_edit_above_block(rendered["body"], "AC1:", "AC9:"),
            )
        )
    )
    assert [f["code"] for f in drifted["findings"]] == ["rendered_text_drift"]
