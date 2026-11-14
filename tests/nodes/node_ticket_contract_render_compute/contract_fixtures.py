# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schema 1.1.0 contracts the render tests run over, built through the core draft model."""

from __future__ import annotations

import copy
from typing import Any

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.models.ticket.model_ticket_draft import ModelTicketDraft

_BRIEF: dict[str, str] = {
    "what_this_is": "A small building block that renders a ticket.",
    "why_it_matters": "The ticket and the file must be exact copies.",
    "done_looks_like": "The rendered body passes the create gate.",
    "risk": "None beyond the node itself.",
    "depends_on": "The standard form in the core model.",
}

_CODE_ONLY: dict[str, Any] = {
    "title": "A fixture ticket that proves code only",
    "business_brief": _BRIEF,
    "parent_ticket_id": "OMN-90000",
    "parent_ac_label": "AC1",
    "repository": "omnimarket",
    "proof_class": "code-only",
    "summary": "fixture",
    "requirements": [
        {
            "id": "REQ-1",
            "statement": "The unit holds.",
            "acceptance": [
                {
                    "id": "AC1",
                    "statement": "the unit holds for every fixture",
                    "falsifier": "uv run pytest tests/x.py -q -k unit",
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
            "checks": [{"check_type": "test_passes", "check_value": "tests/x.py"}],
        }
    ],
}


def _deployed() -> dict[str, Any]:
    data = copy.deepcopy(_CODE_ONLY)
    data["title"] = "A fixture ticket that proves a running service"
    data["proof_class"] = "deployed"
    data["parent_ac_label"] = "AC-3"
    data["requirements"][0]["acceptance"] += [
        {
            "id": "AC2",
            "statement": "done only when running and observed where it runs",
            "falsifier": "one request on the dev bus and its reply",
            "integrated": True,
        },
        {
            "id": "DOD1",
            "statement": "the change\nspans two lines in its source",
            "falsifier": "uv run pytest tests/y.py -q",
            "integrated": False,
        },
    ]
    data["dod_evidence"].append(
        {
            "id": "dod-002",
            "description": "running",
            "binds_ac": ["AC2"],
            "checks": [{"check_type": "endpoint", "check_value": "http://host/health"}],
        }
    )
    return data


def _probed() -> dict[str, Any]:
    data = copy.deepcopy(_CODE_ONLY)
    data["title"] = "A fixture ticket that starts In Progress"
    data["start_probe"] = "git log -1 => the branch has a commit"
    data["diagnosis"] = "the earlier node refused every body"
    return data


def _fenced() -> dict[str, Any]:
    data = copy.deepcopy(_CODE_ONLY)
    data["title"] = "A fixture whose text holds a fence"
    data["summary"] = "a literal fence ```` inside the contract text"
    return data


_FIXTURES: dict[str, dict[str, Any]] = {
    "code_only": _CODE_ONLY,
    "deployed": _deployed(),
    "probed": _probed(),
    "fenced": _fenced(),
}

# The fixture that starts In Progress, so the gate's probe rule runs on it.
IN_PROGRESS = frozenset({"probed"})


def fixture_contracts() -> dict[str, ModelTicketContract]:
    """Every fixture as a contract with its fingerprint set, keyed by name."""
    return {
        name: ModelTicketContract.from_draft(
            ModelTicketDraft.model_validate(data), f"OMN-9{index:04d}"
        )
        for index, (name, data) in enumerate(_FIXTURES.items(), start=1)
    }
