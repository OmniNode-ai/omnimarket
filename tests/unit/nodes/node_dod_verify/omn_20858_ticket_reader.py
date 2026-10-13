# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20858 -- recorded ticket payloads for the dod_verify criteria check.

Tests inject a recorded ticket body through ``ProtocolDodTicketCriteriaReader``,
so no test makes a live Linear call. ``RecordedTicketReader`` serves one recorded
body per read, in order, repeating the last; a ``None`` body is a ticket that
could not be read. ``ticket_matching_contract`` is for the verifier suites that
exercise other behaviour: it rewrites a contract file's pins to the hash of the
text a generated ticket body carries and returns the reader for that body, so the
ticket is unchanged since acceptance and the suite sees the verdict it saw before
the criteria check existed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)
from omnimarket.occ_contract_pin import canonical_ac_label, criterion_hash


class RecordedTicketReader:
    """Serves recorded ticket bodies in order; the last one repeats."""

    def __init__(self, bodies: Sequence[str | None]) -> None:
        assert bodies, "a reader needs at least one recorded read"
        self._bodies = list(bodies)
        self.reads = 0

    def read(self, ticket_id: str) -> ModelTicketCriteriaRead:
        body = self._bodies[min(self.reads, len(self._bodies) - 1)]
        self.reads += 1
        if body is None:
            return ModelTicketCriteriaRead(
                ticket_id=ticket_id, unavailable_reason="recorded: ticket unreadable"
            )
        return ModelTicketCriteriaRead(ticket_id=ticket_id, description=body)


def criteria_body(criteria: Mapping[str, str], *, preamble: str = "") -> str:
    """A ticket body whose acceptance-criteria section lists ``criteria`` in order."""
    lines = [preamble, "## Acceptance criteria", *(f"- {t}" for t in criteria.values())]
    return "\n".join(line for line in lines if line) + "\n"


def _text_for(label: str, statement: str | None) -> str:
    text = re.sub(r"\s+", " ", statement or "recorded criterion").strip()
    return text if canonical_ac_label(text) == label else f"{label}: {text}"


def ticket_matching_contract(path: Path) -> RecordedTicketReader:
    """Pin the contract at ``path`` to a generated ticket body and serve that body."""
    try:
        contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # A contract the verifier refuses to parse never reaches the ticket read.
        return RecordedTicketReader([""])
    statements: dict[str, str | None] = {}
    for requirement in contract.get("requirements") or []:
        for criterion in requirement.get("acceptance") or []:
            label = canonical_ac_label(str(criterion.get("id") or ""))
            if label:
                statements.setdefault(label, criterion.get("statement"))
    for item in contract.get("dod_evidence") or []:
        for claimed in item.get("binds_ac") or []:
            statements.setdefault(canonical_ac_label(str(claimed)), None)
        for record in item.get("ac_bindings") or []:
            statements.setdefault(canonical_ac_label(str(record.get("label"))), None)
    texts = {
        label: _text_for(label, statement)
        for label, statement in statements.items()
        if label
    }
    for item in contract.get("dod_evidence") or []:
        for record in item.get("ac_bindings") or []:
            label = canonical_ac_label(str(record.get("label")))
            if label in texts:
                record["criterion_hash"] = criterion_hash(texts[label])
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return RecordedTicketReader([criteria_body(texts)])
