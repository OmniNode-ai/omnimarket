# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""contracts/OMN-24687.yaml lists every criterion and binds each check to what it proves."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24687.yaml"


def _load() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT.read_text())  # type: ignore[no-any-return]


def _items() -> list[dict[str, object]]:
    return list(_load()["dod_evidence"])  # type: ignore[call-overload]


@pytest.mark.unit
def test_arch_graph_bolt_contract_lists_all_five_criteria() -> None:
    contract = _load()
    ids = [
        ac["id"]
        for req in contract["requirements"]  # type: ignore[attr-defined]
        for ac in req["acceptance"]
    ]
    assert ids == ["AC1", "AC2", "AC3", "AC4", "AC5"]


@pytest.mark.unit
def test_arch_graph_bolt_host_overlay_test_binds_ac2_only() -> None:
    bound = [
        item
        for item in _items()
        if any(
            "test_arch_graph_bolt_host_overlay" in str(chk["check_value"])
            for chk in item["checks"]  # type: ignore[attr-defined]
        )
    ]
    assert len(bound) == 1
    assert bound[0]["binds_ac"] == ["AC2"]


@pytest.mark.unit
def test_arch_graph_bolt_ac3_is_left_to_the_overlays_test() -> None:
    bound = [item for item in _items() if "AC3" in item.get("binds_ac", [])]  # type: ignore[operator]
    assert len(bound) == 1
    values = [str(chk["check_value"]) for chk in bound[0]["checks"]]  # type: ignore[attr-defined]
    assert all("OmniNode-ai/overlays" in value for value in values)
    assert all("test_arch_graph_bolt_host_overlay" not in value for value in values)


@pytest.mark.unit
def test_arch_graph_bolt_unmet_criteria_are_carried_unmet() -> None:
    unmet = [item for item in _items() if item.get("binds_ac") == ["AC1", "AC4", "AC5"]]
    assert len(unmet) == 1
    assert unmet[0]["execution_scope"] == "local_done_gate"
    # reads red until a Bolt service answers: a TCP connect, never a printed string
    values = [str(chk["check_value"]) for chk in unmet[0]["checks"]]  # type: ignore[attr-defined]
    assert any("create_connection" in value for value in values)


@pytest.mark.unit
@pytest.mark.parametrize("host", ["h051", "h053"])
def test_arch_graph_bolt_integration_proof_is_pinned_to_each_lab_host(
    host: str,
) -> None:
    checks = [
        str(chk["check_value"])
        for item in _items()
        if "integration" in str(item["id"])
        for chk in item["checks"]  # type: ignore[attr-defined]
    ]
    pinned = [value for value in checks if f"--host {host} " in value]
    assert len(pinned) == 1
    assert "grep -q ^bolt" in pinned[0]
    assert "ARCH_GRAPH_BOLT_URI" in pinned[0]


@pytest.mark.unit
def test_arch_graph_bolt_no_integration_check_is_unpinned() -> None:
    for item in _items():
        if "integration" not in str(item["id"]):
            continue
        for chk in item["checks"]:  # type: ignore[attr-defined]
            assert "--host h0" in str(chk["check_value"])
