# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the standard ticket contract into the ticket body, and parse it back (OMN-23759).

The ticket in Linear and the contract file must be exact copies, so one renderer turns the same
contract into the same text, and a parser reports a body that was changed by hand.

The body is, in order: the filing lines (the lane stamp and the correlation stamp, which are not
contract content and which the caller passes in), the business brief under ``## In plain terms``,
the ``Gate:`` line, the ``Diagnosis:`` line when set, the labelled criteria with their falsifiers
and ``integrated`` marked, the ``Probe:`` line when set, and one fenced block whose info string is
``yaml onex-ticket-contract`` holding the contract's ``to_yaml()`` output and nothing else. A
``## Probe`` heading opens the probe line and a ``## Contract`` heading opens the block: the create
gate reads criteria until the next line that starts with ``#``, so the headings keep it out of the
block, whose ``acceptance:`` key it would otherwise read as a criteria heading.

``parse`` reads the block and reports, by name:

* ``contract_not_synced``: the block's ``contract_fingerprint`` is not the fingerprint recomputed
  from the block's own contract, so the block was changed after its hash was written;
* ``rendered_text_drift``: the text between the filing lines and the block differs from a fresh
  render of the block's contract, after only the caller's normalizations. The filing lines are
  never compared.

``render`` refuses to emit text that does not parse back with the same fingerprint and no finding.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates
from pydantic import ValidationError

from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderFinding,
    EnumTicketContractRenderOperation,
    EnumTicketContractRenderOutcome,
    ModelTicketContractRenderFinding,
    ModelTicketContractRenderNormalization,
    ModelTicketContractRenderRequest,
    ModelTicketContractRenderResult,
)

_Finding = EnumTicketContractRenderFinding

BLOCK_INFO_STRING = "yaml onex-ticket-contract"
BRIEF_HEADING = "## In plain terms"
CRITERIA_HEADING = "## Acceptance criteria"
PROBE_HEADING = "## Probe"
CONTRACT_HEADING = "## Contract"
_FENCE_OPEN = re.compile(r"^(`{3,})[ \t]*(.*?)[ \t]*$")
_AC_LABEL = re.compile(r"(?i)^AC[-_ ]?(\d+)$")
_BRIEF_LABELS = (
    ("what_this_is", "What this is"),
    ("why_it_matters", "Why it matters"),
    ("done_looks_like", "Done looks like"),
    ("risk", "Risk"),
    ("depends_on", "Depends on"),
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _gate_label(label: str) -> str:
    """``AC1`` and ``ac 1`` become the ``AC-1`` the Gate line's parent form reads."""
    match = _AC_LABEL.match(label.strip())
    return f"AC-{int(match.group(1))}" if match else label.strip()


def _criterion_line(
    criterion_id: str, statement: str, falsifier: str | None, integrated: bool
) -> str:
    text = _one_line(statement)
    if integrated and not text.lower().startswith("integrated:"):
        text = f"integrated: {text}"
    line = f"- [ ] {criterion_id.strip()}: {text}"
    if falsifier:
        line += f" -- falsifier: {_one_line(falsifier)}"
    return line


def render_human_text(contract: ModelTicketContract) -> str:
    """The text between the filing lines and the block; a pure function of the contract."""
    paragraphs: list[str] = []
    brief = contract.business_brief
    if brief is not None:
        paragraphs.append(BRIEF_HEADING)
        for field, label in _BRIEF_LABELS:
            paragraphs.append(f"**{label}:** {getattr(brief, field).strip()}")
    if contract.parent_ticket_id and contract.parent_ac_label:
        paragraphs.append(
            f"Gate: {contract.parent_ticket_id.strip()} {_gate_label(contract.parent_ac_label)}"
        )
    if contract.diagnosis and contract.diagnosis.strip():
        paragraphs.append(f"Diagnosis: {_one_line(contract.diagnosis)}")
    criteria = [
        _criterion_line(c.id, c.statement, c.falsifier, c.integrated)
        for requirement in contract.requirements
        for c in requirement.acceptance
    ]
    if criteria:
        paragraphs.append(CRITERIA_HEADING)
        paragraphs.append("\n".join(criteria))
    if contract.start_probe and contract.start_probe.strip():
        paragraphs.append(PROBE_HEADING)
        paragraphs.append(f"Probe: {_one_line(contract.start_probe)}")
    # A line that opens with '#' is where the create gate's criteria reader stops. Without it the
    # reader would carry on into the block and read the YAML key ``acceptance:`` as a criteria
    # heading and the YAML list items under it as criteria with no falsifier.
    paragraphs.append(CONTRACT_HEADING)
    return "\n\n".join(paragraphs)


def render_block(contract: ModelTicketContract) -> str:
    """The one fenced block: only ``to_yaml()``, fenced longer than any backtick run inside it."""
    text = contract.to_yaml().rstrip("\n")
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{BLOCK_INFO_STRING}\n{text}\n{fence}"


def _compose(contract: ModelTicketContract, filing_lines: Sequence[str]) -> str:
    parts = []
    filing = "\n".join(filing_lines).strip("\n")
    if filing:
        parts.append(filing)
    human = render_human_text(contract)
    if human:
        parts.append(human)
    parts.append(render_block(contract))
    return "\n\n".join(parts) + "\n"


def _normalize(
    text: str, normalizations: Sequence[ModelTicketContractRenderNormalization]
) -> str:
    for rewrite in normalizations:
        text = re.sub(rewrite.pattern, rewrite.replacement, text)
    return text.strip()


def _finding(code: _Finding, detail: str = "") -> ModelTicketContractRenderFinding:
    return ModelTicketContractRenderFinding(code=code, detail=detail)


def _refused(
    *findings: ModelTicketContractRenderFinding,
) -> ModelTicketContractRenderResult:
    return ModelTicketContractRenderResult(
        outcome=EnumTicketContractRenderOutcome.REFUSED, findings=findings
    )


def _find_block(lines: list[str]) -> tuple[int, int] | ModelTicketContractRenderFinding:
    """The line range of the one contract block, or the finding that says why there is none."""
    opens = []
    for index, line in enumerate(lines):
        match = _FENCE_OPEN.match(line)
        if match and match.group(2) == BLOCK_INFO_STRING:
            opens.append((index, match.group(1)))
    if not opens:
        return _finding(
            _Finding.CONTRACT_BLOCK_MISSING,
            f"no fenced block with the info string '{BLOCK_INFO_STRING}'",
        )
    if len(opens) > 1:
        return _finding(
            _Finding.CONTRACT_BLOCK_DUPLICATED,
            f"{len(opens)} fenced blocks carry the info string '{BLOCK_INFO_STRING}'",
        )
    start, fence = opens[0]
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == fence:
            return start, index
    return _finding(
        _Finding.CONTRACT_BLOCK_UNPARSEABLE, "the contract block is never closed"
    )


def _read_contract(
    yaml_text: str,
) -> ModelTicketContract | ModelTicketContractRenderFinding:
    try:
        data = load_yaml_mapping_no_duplicates(yaml_text, source="contract block")
    except (yaml.YAMLError, ValueError) as exc:
        return _finding(_Finding.CONTRACT_BLOCK_UNPARSEABLE, _one_line(str(exc))[:400])
    try:
        return ModelTicketContract.model_validate(data)
    except ValidationError as exc:
        return _finding(_Finding.CONTRACT_INVALID, _one_line(str(exc))[:600])


def _parse(
    request: ModelTicketContractRenderRequest,
) -> ModelTicketContractRenderResult:
    lines = request.body.split("\n")
    located = _find_block(lines)
    if isinstance(located, ModelTicketContractRenderFinding):
        return _refused(located)
    start, end = located
    contract = _read_contract("\n".join(lines[start + 1 : end]))
    if isinstance(contract, ModelTicketContractRenderFinding):
        return _refused(contract)

    findings: list[ModelTicketContractRenderFinding] = []
    stated = contract.contract_fingerprint or ""
    recomputed = contract.compute_fingerprint()
    if stated != recomputed:
        findings.append(
            _finding(
                _Finding.CONTRACT_NOT_SYNCED,
                f"the block states fingerprint '{stated or 'none'}'; its contract recomputes to '{recomputed}'",
            )
        )

    expected = render_human_text(contract)
    before = lines[:start]
    anchor = expected.split("\n", 1)[0] if expected else ""
    anchor_index = next(
        (i for i, line in enumerate(before) if anchor and line.strip() == anchor), None
    )
    if anchor_index is None:
        filing, actual = before, ""
    else:
        filing, actual = before[:anchor_index], "\n".join(before[anchor_index:])
    if _normalize(actual, request.normalizations) != _normalize(
        expected, request.normalizations
    ):
        findings.append(
            _finding(
                _Finding.RENDERED_TEXT_DRIFT,
                "the text above the block differs from a fresh render of the block's contract"
                if anchor_index is not None or not expected
                else f"the text above the block never opens with '{anchor}'",
            )
        )
    if "\n".join(lines[end + 1 :]).strip():
        findings.append(
            _finding(_Finding.RENDERED_TEXT_DRIFT, "text follows the contract block")
        )
    return ModelTicketContractRenderResult(
        outcome=EnumTicketContractRenderOutcome.PARSED,
        filing_lines=tuple("\n".join(filing).strip("\n").split("\n"))
        if "\n".join(filing).strip("\n")
        else (),
        contract=contract,
        block_fingerprint=stated,
        recomputed_fingerprint=recomputed,
        findings=tuple(findings),
    )


def _render(
    request: ModelTicketContractRenderRequest,
) -> ModelTicketContractRenderResult:
    contract = request.contract
    if contract is None:  # the request model already refuses this
        return _refused(
            _finding(_Finding.CONTRACT_INVALID, "a render request carries a contract")
        )
    body = _compose(contract, request.filing_lines)
    back = _parse(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.PARSE,
            body=body,
            normalizations=request.normalizations,
        )
    )
    if back.outcome is EnumTicketContractRenderOutcome.REFUSED:
        return _refused(*back.findings)
    if back.findings:
        return _refused(*back.findings)
    return ModelTicketContractRenderResult(
        outcome=EnumTicketContractRenderOutcome.RENDERED,
        body=body,
        filing_lines=back.filing_lines,
        contract=back.contract,
        block_fingerprint=back.block_fingerprint,
        recomputed_fingerprint=back.recomputed_fingerprint,
    )


class HandlerTicketContractRender:
    """Render a contract into a ticket body, or parse a body back, from the request alone."""

    def handle(
        self, request: ModelTicketContractRenderRequest
    ) -> ModelTicketContractRenderResult:
        if request.operation is EnumTicketContractRenderOperation.RENDER:
            return _render(request)
        return _parse(request)
