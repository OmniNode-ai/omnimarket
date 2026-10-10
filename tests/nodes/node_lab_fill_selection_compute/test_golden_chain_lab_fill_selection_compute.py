# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute every operation in the packaged selection contract."""

from __future__ import annotations

import importlib
from importlib.resources import files

import yaml

from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    ModelApprovedWorkEvidence,
    ModelApprovedWorkTicketFacts,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
)


def test_golden_chain_lab_fill_selection_compute() -> None:
    name = "node_lab_fill_selection_compute"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert contract["node_type"] == "compute"
    assert contract["lifecycle"] == "experimental"
    assert "event_bus" not in contract
    assert "terminal_event" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {entry["operation"] for entry in routing["handlers"]} == {
        "select_lab_fill_work",
        "mark_approved_work_done",
    }
    deployment = ModelLabFillDeployment.from_overlay(
        {
            "companion_repositories": ["change_control"],
            "not_work_repositories": ["registry"],
            "document_repositories": ["docs"],
        }
    )
    for entry in routing["handlers"]:
        handler_type = getattr(
            importlib.import_module(entry["handler"]["module"]),
            entry["handler"]["name"],
        )
        input_module, _, input_name = entry["input_model"].rpartition(".")
        output_module, _, output_name = entry["output_model"].rpartition(".")
        request_type = getattr(importlib.import_module(input_module), input_name)
        result_type = getattr(importlib.import_module(output_module), output_name)
        if entry["operation"] == "select_lab_fill_work":
            request = request_type(
                now="2026-10-07T00:00:00Z",
                candidates=(
                    ModelLabFillCandidateInput(
                        "ticket", "ticket", "OMN-1", ticket_prs=("repo_a#1",)
                    ),
                ),
                ledger_lines=(),
                deployment=deployment,
            )
            result = handler_type().handle(request)
            assert result.decisions[0].reason is None
            assert result.decisions[0].repo == "repo_a"
        else:
            request = request_type(
                rows=({"id": "row", "ticket": "OMN-1"},),
                facts=(
                    ModelApprovedWorkTicketFacts(
                        "OMN-1",
                        acceptance_status="pass",
                        evidence=(ModelApprovedWorkEvidence("repo_a#1", "a" * 40),),
                    ),
                ),
                checked_at="2026-10-07T00:00:00Z",
            )
            result = handler_type().handle(request)
            assert result.rows[0]["done"] is True
        assert isinstance(request, request_type)
        assert isinstance(result, result_type)
