# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deploy probes preserve rejection evidence without binding a criterion."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_deploy_probe_keeps_ac10_bound_to_rejection_tests() -> None:
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "OMN-20074.yaml").read_text(encoding="utf-8")
    )
    evidence = {item["id"]: item for item in contract["dod_evidence"]}
    probe = evidence["dod-omnimarket-pr-3775-deploy-probe"]
    rejection = evidence["dod-omnimarket-pr-3775-rejection"]

    assert "binds_ac" not in probe, (
        "the command-only deploy probe must not bind an acceptance criterion"
    )
    assert probe["checks"], "the deploy probe must remain in the evidence"
    assert all(check["check_type"] == "command" for check in probe["checks"])
    assert rejection["binds_ac"] == ["AC10"]
    assert any(
        check["check_type"] == "test_passes"
        and check["check_value"]
        == "uv run pytest tests/unit/nodes/node_reject_skip_token_compute/test_reject_skip_token.py -q -p no:randomly"
        for check in rejection["checks"]
    ), "AC10 must retain the rejection test evidence"


def test_omn19401_step_by_step_selection_names_its_test_file() -> None:
    contract = yaml.safe_load(
        (REPO_ROOT / "contracts" / "OMN-19401.yaml").read_text(encoding="utf-8")
    )
    evidence = {item["id"]: item for item in contract["dod_evidence"]}
    selection = evidence["dod-omn19401-ac1-step-by-step-selection"]

    assert selection["binds_ac"] == ["AC1"]
    assert [check["check_value"] for check in selection["checks"]] == [
        "uv run pytest tests/unit/nodes/node_delegation_quality_gate_reducer/"
        "test_step_by_step_explanation_causal_markers_omn19401.py -k step_by_step -q"
    ], "AC1's step_by_step selection must name the reducer test file it runs"
