# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 of the OMN-24918 contract is bound to the lab command that runs the real-Postgres file (OMN-24918 contract)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def _commands(contract: str, item_id: str) -> list[str]:
    document = yaml.safe_load((_CONTRACTS / contract).read_text(encoding="utf-8"))
    item = next(i for i in document["dod_evidence"] if i["id"] == item_id)
    return [str(check["check_value"]).strip() for check in item["checks"]]


@pytest.mark.unit
def test_omn24918_contract_ac7_is_the_lab_work_command_of_the_real_postgres_item() -> (
    None
):
    ac7 = _commands("OMN-24918.yaml", "dod-omn24918-ac7")
    postgres = _commands("OMN-24874.yaml", "dod-omn24874-real-postgres")
    assert ac7 == postgres
    command = ac7[0]
    assert "onex lab-work run" in command
    assert "test_omn24874_tenant_overlay_cost_real_postgres.py" in command
    # A run that skips any of the file's tests fails the command, and its exit code is the pytest run's.
    assert "grep -q skipped" in command
    assert "exit $R" in command


@pytest.mark.unit
def test_omn24918_contract_ac7_is_not_a_text_match_test_of_the_contract() -> None:
    # The old binding ran a pytest that only matched text in the contract: it passed when the Postgres run failed or skipped.
    for command in _commands("OMN-24918.yaml", "dod-omn24918-ac7"):
        assert "test_omn24874_contract_checks.py" not in command
        assert "-k real_postgres" not in command
