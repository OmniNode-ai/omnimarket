# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store declares one generated password per lab customer identity (OMN-25856).

The identity node refuses a user whose password_key_id the store contract does not declare, so a
second and a third lab customer sign-in each need a declared id of their own, in the dev folder,
under a name that cannot collide with the overlay user's DEV_PROBE_USER_PASSWORD.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
LAB_IDS = {
    "dev_lab_customer_a_password": "DEV_LAB_CUSTOMER_A_PASSWORD",
    "dev_lab_customer_b_password": "DEV_LAB_CUSTOMER_B_PASSWORD",
}


def _generated() -> dict[str, dict[str, object]]:
    return yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"]


@pytest.mark.parametrize(("key_id", "key_name"), sorted(LAB_IDS.items()))
def test_lab_customer_password_declared(key_id: str, key_name: str) -> None:
    entry = _generated()[key_id]
    assert entry["folder"] == "/shared/env"
    assert entry["key"] == key_name
    assert entry["prefix"] == "Dp1!"


def test_lab_customer_keys_do_not_share_a_secret_name() -> None:
    names = [entry["key"] for entry in _generated().values()]
    assert len(names) == len(set(names))


def test_failing_control_undeclared_id_is_absent() -> None:
    assert "dev_lab_customer_c_password" not in _generated()
