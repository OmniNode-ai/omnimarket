# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract of node_host_drift_owner_dispatch_effect names its handler, its topics and the overlay it reads (OMN-24239)."""

from __future__ import annotations

import importlib

import pytest
import yaml

from .support import NODE

pytestmark = pytest.mark.unit


def test_the_contract_names_a_handler_that_imports_and_the_topics_it_declares() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert hasattr(module, contract["handler"]["class"])
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.host-drift-owner-dispatch-completed.v1"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["topics"]["dispatch_requested"]
    )
    assert contract["overlay"] == {
        "node": "node_host_drift_owner_dispatch_effect",
        "roots_env": "ONEX_SKILL_OVERLAY_ROOTS",
    }
    for test in contract["dod_evidence"]:
        assert (NODE.parents[3] / test["test"]).is_file(), test["test"]
