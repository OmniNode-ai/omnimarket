# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deploy probes preserve Codex headroom evidence without binding a criterion."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_deploy_probe_keeps_ac28_bound_to_codex_headroom_tests() -> None:
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "OMN-17427.yaml").read_text(encoding="utf-8")
    )
    evidence = {item["id"]: item for item in contract["dod_evidence"]}
    probe = evidence["dod-omn17427-ac28-tick-codex-headroom-deploy-probe"]
    headroom = evidence["dod-omn17427-ac28-tick-codex-headroom"]

    assert "binds_ac" not in probe, (
        "the command-only deploy probe must not bind an acceptance criterion"
    )
    assert probe["checks"], "the deploy probe must remain in the evidence"
    assert all(check["check_type"] == "command" for check in probe["checks"])
    assert headroom["binds_ac"] == ["AC28"]
    assert any(
        check["check_type"] == "test_passes"
        and check["check_value"]
        == "uv run pytest tests/nodes/node_throughput_tick_decision_compute/test_codex_headroom_throughput_tick_decision_compute.py -q"
        for check in headroom["checks"]
    ), "AC28 must retain the Codex headroom test evidence"
