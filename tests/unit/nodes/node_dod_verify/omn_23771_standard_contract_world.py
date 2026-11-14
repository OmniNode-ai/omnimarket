# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contracts for the OMN-23771 standard-form tests.

``standard_contract`` builds a schema 1.1.0 contract the core model accepts and
stamps its fingerprint the way the ticket node does; ``legacy_contract`` builds
the 1.0.0 shape the collector has always read.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
)

_STANDARD_FORM: dict[str, Any] = {
    "schema_version": "1.1.0",
    "ticket_id": "OMN-90001",
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
    "parent_ticket_id": "OMN-90000",
    "parent_ac_label": "AC1",
    "repository": "omnimarket",
    "requirements": [
        {
            "id": "REQ-1",
            "statement": "r",
            "acceptance": [
                {
                    "id": "AC1",
                    "statement": "unit",
                    "falsifier": "true",
                    "integrated": False,
                }
            ],
        }
    ],
    "dod_evidence": [
        {
            "id": "dod-001",
            "description": "unit",
            "binds_ac": ["AC1"],
            "checks": [{"check_type": "command", "check_value": "true"}],
        }
    ],
}


def standard_contract(ticket_id: str = "OMN-90001", **overrides: Any) -> dict[str, Any]:
    """A 1.1.0 contract as the ticket node writes it: fingerprint included."""
    data = copy.deepcopy(_STANDARD_FORM)
    data["ticket_id"] = ticket_id
    data.update(overrides)
    model = ModelTicketContract.model_validate(data)
    model.update_fingerprint()
    return model.model_dump(mode="json")


def legacy_contract(ticket_id: str, **overrides: Any) -> dict[str, Any]:
    """A 1.0.0 contract the core model accepts."""
    data: dict[str, Any] = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "title": "A legacy ticket",
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "runs",
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        ],
    }
    data.update(overrides)
    return data


def write_contract(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def subject_for(repository: str) -> ModelDodContractSubject:
    """A bound contract subject naming ``repository`` as the repository."""
    return ModelDodContractSubject(
        source=EnumDodContractSource.PRODUCT_REPOSITORY,
        repository=repository,
        commit_sha="a" * 40,
        repo_path="contracts/OMN-90001.yaml",
    )
