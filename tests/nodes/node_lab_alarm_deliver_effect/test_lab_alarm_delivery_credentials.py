# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credentials declared by the lab alarm delivery contract, and the controls that fail if they are removed (OMN-23906).

Golden: the contract's secrets mapping declares exactly SLACK_BOT_TOKEN and SLACK_CHANNEL_ID, each with a non-empty
description and required false, and no value-like key. Failing controls: the undeclared-refs helper is empty for the
real contract, names both refs when the secrets block is removed, and names the one missing ref when only the other
is declared. Error: no string value anywhere in the contract carries a Slack bot token.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect/contract.yaml"
)
_SECRETS = ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID")
_TOKEN = re.compile(r"xoxb-[A-Za-z0-9-]+")


def _load_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _secrets_mapping(contract: dict[str, Any]) -> dict[str, Any]:
    candidate = contract.get("secrets")
    return candidate if isinstance(candidate, dict) else {}


def _undeclared_refs(contract: dict[str, Any]) -> list[str]:
    return sorted(name for name in _SECRETS if name not in _secrets_mapping(contract))


def _string_values(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        values: list[str] = []
        for child in node.values():
            values.extend(_string_values(child))
        return values
    if isinstance(node, list):
        values = []
        for child in node:
            values.extend(_string_values(child))
        return values
    return []


def _copy_without_secrets() -> dict[str, Any]:
    contract = copy.deepcopy(_load_contract())
    del contract["secrets"]
    return contract


def _copy_with_only_bot_token() -> dict[str, Any]:
    contract = copy.deepcopy(_load_contract())
    secrets = _secrets_mapping(contract)
    contract["secrets"] = {"SLACK_BOT_TOKEN": secrets["SLACK_BOT_TOKEN"]}
    return contract


def test_lab_alarm_delivery_credentials_golden_refs_declared_as_optional() -> None:
    secrets = _secrets_mapping(_load_contract())
    assert set(secrets) == set(_SECRETS)
    for name, entry in secrets.items():
        assert name in _SECRETS
        assert set(entry) == {"description", "required"}
        assert isinstance(entry["description"], str)
        assert entry["description"].strip()
        assert entry["required"] is False


def test_lab_alarm_delivery_credentials_failing_control_real_contract_empty() -> None:
    assert _undeclared_refs(_load_contract()) == []


def test_lab_alarm_delivery_credentials_failing_control_removed_names_both() -> None:
    assert _undeclared_refs(_copy_without_secrets()) == list(_SECRETS)


def test_lab_alarm_delivery_credentials_failing_control_partial_names_one() -> None:
    assert _undeclared_refs(_copy_with_only_bot_token()) == ["SLACK_CHANNEL_ID"]


def test_lab_alarm_delivery_credentials_error_no_token_in_any_string_value() -> None:
    leaked = [
        value for value in _string_values(_load_contract()) if _TOKEN.search(value)
    ]
    assert leaked == []
