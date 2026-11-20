# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures for the standard-ticket rules of dod-verify (OMN-23771)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

CUTOVER_FIRST_TICKET = 1000
AT_CUTOVER = "OMN-1000"
BELOW_CUTOVER = "OMN-999"
REPOSITORY = "omnimarket"


def write_overlay(root: Path) -> Path:
    """An overlay root whose node_dod_verify overlay names the cutover."""
    overlay = root / "node_dod_verify" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        'standard_ticket_cutover_date: "2026-11-20"\n'
        f'standard_ticket_first_ticket_number: "{CUTOVER_FIRST_TICKET}"\n',
        encoding="utf-8",
    )
    return root


def standard_contract(
    ticket_id: str = AT_CUTOVER, command: str = "echo ok"
) -> dict[str, Any]:
    """A schema 1.1.0 contract the core model accepts, whose one check runs ``command``."""
    return {
        "schema_version": "1.1.0",
        "ticket_id": ticket_id,
        "title": "A standard ticket",
        "summary": "s",
        "proof_class": "code-only",
        "business_brief": {
            "what_this_is": "a",
            "why_it_matters": "b",
            "done_looks_like": "c",
            "risk": "d",
            "depends_on": "e",
        },
        "parent_ticket_id": "OMN-1",
        "parent_ac_label": "AC1",
        "repository": REPOSITORY,
        "requirements": [
            {
                "id": "REQ-1",
                "statement": "r",
                "acceptance": [
                    {
                        "id": "AC1",
                        "statement": "the command exits 0",
                        "falsifier": "the command exits non-zero",
                        "integrated": False,
                    }
                ],
            }
        ],
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "the command exits 0",
                "binds_ac": ["AC1"],
                "checks": [{"check_type": "command", "check_value": command}],
            }
        ],
    }


def synced_yaml(data: dict[str, Any]) -> str:
    """The contract as the ticket node writes it: its fingerprint set to its hash."""
    contract = ModelTicketContract.model_validate(copy.deepcopy(data))
    contract.update_fingerprint()
    return contract.to_yaml()


def write_contract(
    checkout: Path, text: str, ticket_id: str = AT_CUTOVER, name: str | None = None
) -> Path:
    """Write ``contracts/<ticket>.yaml`` under a checkout named for its repository."""
    path = checkout / "contracts" / (name or f"{ticket_id}.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def edited_after_sync(data: dict[str, Any]) -> str:
    """A synced contract whose title was edited after its fingerprint was written."""
    edited = yaml.safe_load(synced_yaml(data))
    edited["title"] = "An edited title"
    return yaml.safe_dump(edited, sort_keys=False)
