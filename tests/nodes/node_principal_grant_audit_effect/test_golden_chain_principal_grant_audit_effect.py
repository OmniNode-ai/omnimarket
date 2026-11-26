# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged grant audit contract with a fake runner (OMN-21622)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_principal_grant_audit_effect.models import (
    ModelPrincipalGrantAuditDeployment,
)

from .test_principal_grant_audit_effect import FakeRunner

pytestmark = pytest.mark.unit


def test_golden_chain_principal_grant_audit_effect(tmp_path: Path) -> None:
    name = "node_principal_grant_audit_effect"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert contract["node_type"] == "effect"
    assert contract["lifecycle"] == "experimental"
    assert "event_bus" not in contract
    assert "terminal_event" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {entry["operation"] for entry in routing["handlers"]} == {
        "run_principal_grant_audit"
    }
    deployment = ModelPrincipalGrantAuditDeployment(
        infra_checkout="infra",
        script="scripts/audit.py",
        grants_file="grants.yaml",
        issuer_container="container-a",
        overlay_mount_target="/overlay/doc.yaml",
        ledger_file="ledger.jsonl",
        role="machine",
        timeout_seconds=300,
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
        runner = FakeRunner(tmp_path)
        handler = handler_type(overlay=deployment, run=runner.run, python="python")
        request = request_type(registry_root=tmp_path)
        result = handler.handle(request)
        assert isinstance(request, request_type)
        assert isinstance(result, result_type)
        assert result.status == "ok"
        assert result.exit_code == 0
        assert [p.principal for p in result.principals] == ["m1"]
        assert [step.step for step in result.steps] == ["inspect", "audit"]
