# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the dev deploy agent's unit reads the file this delivery writes (OMN-24611).

The dev deploy agent lives in another repository; its systemd unit names the
env file through ``EnvironmentFile=`` and reads the variable
``DEPLOY_AGENT_HMAC_SECRET`` from it. Both names are pinned here against the
contract's ``env_file_deliveries`` entry, and the entry's ``key_id`` must be a
``generated_keys`` entry whose stored key name is ``DEPLOY_AGENT_DEV_HMAC_SECRET``
-- the dev-only signing key; the live agent's key is a different name and is
never read or written here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

STORE_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
DELIVERY_ID = "deploy_agent_dev_signing_key"
#: What the dev deploy agent's unit names in EnvironmentFile= (other repository).
UNIT_ENV_FILE = ".omnibase/deploy-agent-dev-signing-key.env"
#: The variable the unit reads; the file's one line is ``<name>=<value>``.
UNIT_ENV_NAME = "DEPLOY_AGENT_HMAC_SECRET"
#: The store key name of the dev-only signing key the contract generates.
STORED_KEY = "DEPLOY_AGENT_DEV_HMAC_SECRET"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))


def _problems(raw: dict[str, Any]) -> list[str]:
    found: list[str] = []
    delivery = raw["env_file_deliveries"][DELIVERY_ID]
    if delivery["home_relative_path"] != UNIT_ENV_FILE:
        found.append(
            f"delivery home_relative_path={delivery['home_relative_path']}, "
            f"unit EnvironmentFile= names {UNIT_ENV_FILE}"
        )
    if delivery["env_name"] != UNIT_ENV_NAME:
        found.append(
            f"delivery env_name={delivery['env_name']}, unit reads {UNIT_ENV_NAME}"
        )
    generated = raw["generated_keys"].get(delivery["key_id"])
    if generated is None or generated["key"] != STORED_KEY:
        found.append(
            f"delivery key_id={delivery['key_id']} is no generated_keys entry "
            f"whose key is {STORED_KEY}"
        )
    return found


def test_seam_holds() -> None:
    assert _problems(_contract()) == []


def test_failing_control_renamed_env_file() -> None:
    raw = _contract()
    raw["env_file_deliveries"][DELIVERY_ID]["home_relative_path"] = (
        ".omnibase/renamed.env"
    )
    assert _problems(raw)


def test_failing_control_renamed_env_name() -> None:
    raw = _contract()
    raw["env_file_deliveries"][DELIVERY_ID]["env_name"] = "RENAMED_HMAC_SECRET"
    assert _problems(raw)


def test_failing_control_renamed_stored_key() -> None:
    raw = _contract()
    raw["generated_keys"][DELIVERY_ID]["key"] = "RENAMED_DEV_HMAC_SECRET"
    assert _problems(raw)
