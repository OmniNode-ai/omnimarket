# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pull request titled OMN-24562 binds its customer-key work in OMN-24562's own contract.

repo-evidence / dod-verify refuses a pull request whose ticket contract carries only
evidence items already on dev. This pins the new criterion and its evidence item.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-24562.yaml"
KEY_TESTS = (
    "tests/test_credential_publisher.py",
    "tests/unit/cli/test_omn24562_revoke_tenant_key_command.py",
    "tests/unit/cli/test_omn21012_register_tenant_key_host.py",
)


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def test_contract_declares_the_customer_key_removal_criterion() -> None:
    ids = {
        ac["id"]
        for req in _contract()["requirements"]
        for ac in req.get("acceptance", [])
    }
    assert "AC7" in ids


def test_a_new_evidence_item_binds_it_to_the_key_tests() -> None:
    items = [
        i for i in _contract()["dod_evidence"] if "AC7" in (i.get("binds_ac") or [])
    ]
    assert items, "no dod_evidence item binds AC7"
    commands = " ".join(c["check_value"] for i in items for c in i["checks"])
    for path in KEY_TESTS:
        assert path in commands
