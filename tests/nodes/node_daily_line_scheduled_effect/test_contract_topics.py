# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_daily_line_scheduled_effect declares is a topic it publishes (OMN-25163)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE_DIR = REPO / "src/omnimarket/nodes/node_daily_line_scheduled_effect"
CONTRACT = NODE_DIR / "contract.yaml"
METADATA = NODE_DIR / "metadata.yaml"
RUNNER_CONTRACT = REPO / "src/omnimarket/nodes/node_daily_line_effect/contract.yaml"
OWNER_CONTRACT = REPO / "contracts/OMN-25163.yaml"


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.schedule-run.v1"
    assert (
        "onex.evt.omnimarket.schedule-run.v1" in loaded["event_bus"]["publish_topics"]
    )


def test_subscription_topic_and_operation_are_as_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert len(subs) == 1
    assert subs[0]["topic"] == "onex.intent.platform.runtime-tick.v1"
    assert subs[0]["operation"] == "daily_line.scheduled_tick"


def test_timeout_ms_is_the_runners_own_five_minute_unit_limit() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    runner = yaml.safe_load(RUNNER_CONTRACT.read_text(encoding="utf-8"))
    # One run reads the ledger (60 s bound) and appends one row (60 s bound), plus the floor read and the file write; the
    # runner's own timer unit allows 300 s for the same run, so the scheduled node's limit is exactly that, not a floor.
    assert loaded["descriptor"]["timeout_ms"] == 300_000
    assert (
        loaded["descriptor"]["timeout_ms"]
        == runner["schedule"]["timeout_seconds"] * 1000
    )
    bounds = (
        runner["runtime"]["ledger_read_timeout_seconds"]
        + runner["runtime"]["ledger_append_timeout_seconds"]
    )
    assert loaded["descriptor"]["timeout_ms"] > bounds * 1000 * 2


def test_metadata_declares_the_real_capabilities() -> None:
    metadata = yaml.safe_load(METADATA.read_text(encoding="utf-8"))
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    caps = metadata["capabilities"]
    # The run reads the ledger over the bus, reads the floor record under the registry home and writes daily-line.json
    # and one STATUS row there: it is not network-free, repo-free or side-effect-free.
    assert caps["requires_network"] is True
    assert caps["requires_repo"] is True
    assert caps["side_effect_class"] == "write"
    assert contract["descriptor"]["purity"] == "effectful"
    assert caps["side_effect_class"] != "none"


def test_contract_ac3_is_read_on_dev_and_bound_at_the_hosted_gate_to_this_test() -> (
    None
):
    contract = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    entries = contract["dod_evidence"]
    # The hosted gate refuses a done-gate entry that carries binds_ac, so AC3 is bound to the hosted test_passes entry that
    # runs these tests, and the live read is the done-gate entry named for AC3 (no binds_ac).
    bound = [e for e in entries if "AC3" in e.get("binds_ac", [])]
    assert len(bound) == 1
    assert [c["check_type"] for c in bound[0]["checks"]] == ["test_passes"]
    assert "test_contract_topics.py" in bound[0]["checks"][0]["check_value"]
    reads = [e for e in entries if "ac3" in e["id"] and "read" in e["id"]]
    assert len(reads) == 1
    assert reads[0]["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in reads[0]
    for check in reads[0]["checks"]:
        value = check["check_value"]
        assert "onex.evt.omnimarket.schedule-run.v1" in value
        assert "daily_line" in value
        assert "kcat" in value
        # A grep of contract.yaml on GitHub passes with no deploy and no event; it is not AC3's evidence.
        assert "contract.yaml" not in value
        assert "gh api" not in value


def test_contract_no_entry_hardcodes_the_lane_of_an_earlier_run() -> None:
    contract = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    for entry in contract["dod_evidence"]:
        for check in entry["checks"]:
            assert "run5340" not in check["check_value"]


def test_contract_names_the_overlay_pull_request_order_in_the_ac3_text() -> None:
    contract = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    ac3 = next(
        a for r in contract["requirements"] for a in r["acceptance"] if a["id"] == "AC3"
    )["statement"]
    assert "onex.evt.omnimarket.schedule-run.v1" in ac3
    assert "daily_line" in ac3
    assert "overlays" in ac3
    assert "after this pull request merges" in ac3
