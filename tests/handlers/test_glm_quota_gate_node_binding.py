# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24651: the GLM quota gate handler is bound from a node contract and its command line lives in that node.

The Canonical File Shape job refuses a handler outside a node that no ``contract.yaml`` binds (``handler_outside_node``).
The node that binds the other quota handler, ``node_llm_delegation_call_effect``, binds this one too, and the lane step's
command line is that node's ``runtime_glm_quota_gate`` module.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml

import omnimarket
from omnimarket.handlers.handler_glm_quota_gate import HandlerGlmQuotaGate
from omnimarket.nodes.node_llm_delegation_call_effect import runtime_glm_quota_gate

SRC = Path(omnimarket.__file__).resolve().parent
NODE = SRC / "nodes" / "node_llm_delegation_call_effect"
CONTRACT = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
REPO_ROOT = SRC.parents[1]


def test_a_node_contract_binds_the_gate_handler_and_its_request_model() -> None:
    block = CONTRACT["glm_quota_gate"]
    assert block["handler"] == {
        "name": "HandlerGlmQuotaGate",
        "module": "omnimarket.handlers.handler_glm_quota_gate",
    }
    handler = getattr(
        importlib.import_module(block["handler"]["module"]), block["handler"]["name"]
    )
    assert handler is HandlerGlmQuotaGate
    request = getattr(
        importlib.import_module(block["request_model"]["module"]),
        block["request_model"]["name"],
    )
    assert request.__name__ == "ModelGlmQuotaGateRequest"


def test_the_marker_file_the_contract_names_is_the_one_the_command_line_writes() -> (
    None
):
    assert (
        CONTRACT["glm_quota_gate"]["marker_file"] == runtime_glm_quota_gate.MARKER_NAME
    )


def test_the_contract_states_why_the_gate_does_not_use_provider_quota_state() -> None:
    assert CONTRACT["glm_quota_gate"]["quota_state_not_used_because"]


def test_the_handler_module_holds_no_command_line_or_marker_file() -> None:
    source = (SRC / "handlers" / "handler_glm_quota_gate.py").read_text(
        encoding="utf-8"
    )
    assert "def main" not in source
    assert "sys.argv" not in source
    assert "glm-quota-exhausted" not in source


def test_the_gate_handler_adds_no_baseline_line() -> None:
    for name in (
        "canonical_file_shape_baseline.txt",
        "canonical_file_shape_burndown_baseline.txt",
    ):
        text = (REPO_ROOT / ".onex_ratchets" / name).read_text(encoding="utf-8")
        assert "handler_glm_quota_gate" not in text
        assert "runtime_glm_quota_gate" not in text
