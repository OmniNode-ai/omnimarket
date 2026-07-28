# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_staging_readiness_compute (OMN-15253).

Drives the node through the real native runtime dispatch path — command topic in,
typed verdict out — rather than calling the handler directly, so the contract's
declared command/terminal topics and its def-B input model are exercised as the
runtime actually resolves them.

The snapshots below are hand-authored and are therefore sample data by
construction: this proves the CHAIN, not the environment. A live readiness
verdict requires slice 2's collect EFFECT capturing a real snapshot from the
canonical dev cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.codex.runtime_client import CodexRuntimeRequestAdapter
from tests.integration.node_staging_readiness_compute.canonical_dev_fixtures import (
    contract_payload,
    repaired_snapshot_payload,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_staging_readiness_compute"
    / "contract.yaml"
)
_COMMAND_TOPIC = "onex.cmd.omnimarket.staging-readiness-compute.v1"
_TERMINAL_TOPIC = "onex.evt.omnimarket.staging-readiness-evaluated.v1"
_EVALUATED_AT = "2026-07-27T17:00:00Z"


def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    response = CodexRuntimeRequestAdapter().dispatch_sync(
        command_name="staging_readiness_compute",
        payload=payload,
        runtime_selection="local",
        timeout_ms=30_000,
    )

    assert response.ok is True
    assert response.runtime_mode == "local"
    assert response.runtime_evidence is not None
    assert response.runtime_evidence.node_contract == str(_CONTRACT_PATH)
    assert response.runtime_evidence.command_topic == _COMMAND_TOPIC
    assert response.runtime_evidence.terminal_topic == _TERMINAL_TOPIC
    assert response.output_payloads is not None
    result = response.output_payloads[0]
    assert isinstance(result, dict)
    return result


def _request_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": contract_payload(),
        "snapshot": snapshot,
        "evaluated_at": _EVALUATED_AT,
    }


@pytest.mark.unit
def test_contract_declares_native_runtime_surface() -> None:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert raw.get("node_not_implemented") is None
    assert raw["node_type"] == "compute"
    assert raw["descriptor"]["purity"] == "pure"
    assert raw["handler"]["class"] == "HandlerStagingReadinessCompute"
    assert raw["handler"]["input_model"].endswith("ModelStagingReadinessRequest")
    assert raw["terminal_event"] == _TERMINAL_TOPIC
    assert raw["event_bus"]["subscribe_topics"] == [_COMMAND_TOPIC]
    assert raw["runtime_dispatch"]["command_topic"] == _COMMAND_TOPIC


@pytest.mark.unit
def test_runtime_dispatch_returns_ready_for_the_repaired_composition() -> None:
    result = _dispatch(_request_payload(repaired_snapshot_payload()))

    assert result["status"] == "READY"
    assert result["deployment_permitted"] is True
    assert result["findings"] == []
    assert result["blocking_findings_count"] == 0
    assert result["indeterminate_findings_count"] == 0
    assert result["provenance"]["contract_id"] == "staging-composition.canonical-dev"


@pytest.mark.unit
def test_runtime_dispatch_blocks_on_a_replayed_weekend_defect() -> None:
    """The handler-owning package dropped from the runtime allowlist (F-01 #4)."""
    snapshot = repaired_snapshot_payload()
    snapshot["runtime"]["active_runtime_packages"] = ["omnibase_infra"]

    result = _dispatch(_request_payload(snapshot))

    assert result["status"] == "BLOCKED"
    assert result["deployment_permitted"] is False
    assert result["blocking_findings_count"] >= 1
    checks = {finding["check"] for finding in result["findings"]}
    assert "HANDLER_OWNER_PACKAGES_ACTIVE" in checks


@pytest.mark.unit
def test_runtime_dispatch_is_indeterminate_when_nothing_was_observed() -> None:
    """Fail-closed over the wire: an empty snapshot never renders READY."""
    result = _dispatch(_request_payload({"captured_at": "2026-07-27T16:55:00Z"}))

    assert result["status"] == "INDETERMINATE"
    assert result["deployment_permitted"] is False
    assert result["blocking_findings_count"] == 0
    assert result["indeterminate_findings_count"] >= 1
