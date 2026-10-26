# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21423: the GitHub write and gateway nodes may be answered by any runtime.

The write effect ran on one runtime only; when that runtime stopped, every pull
request write in the fleet stopped with it. Both GitHub nodes now declare
``placement: {mode: any}`` so every runtime that loads them joins the one
consumer group the runtime derives for the node (the group id carries no host
part), and any member answers.

The block declares a mode and nothing else. A consumer group is runtime-owned
and never contract-declared (``test_consumer_group_contract_declaration_strip``),
so a ``placement.consumer_group`` key is refused here as well.

Each runtime reads the token from the contract-declared ``GITHUB_TOKEN`` ref
through the secret store resolver; the second test pins that declaration as
required on both nodes, because it is the record a runtime with no token is
kept out of the group by.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODES_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"
_GITHUB_NODES = (
    "node_github_repo_write_effect",
    "node_github_repo_gateway_effect",
)


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((_NODES_ROOT / node / "contract.yaml").read_text())
    assert isinstance(raw, dict), f"{node}/contract.yaml is not a mapping"
    return raw


@pytest.mark.parametrize("node", _GITHUB_NODES)
def test_github_node_declares_placement_mode_any(node: str) -> None:
    placement = _contract(node).get("placement")
    assert placement == {"mode": "any"}, (
        f"{node} must declare placement: {{mode: any}} so any runtime answers; "
        f"read {placement!r}"
    )


@pytest.mark.parametrize("node", _GITHUB_NODES)
def test_github_node_declares_required_github_token(node: str) -> None:
    secrets = _contract(node).get("secrets")
    assert isinstance(secrets, dict), f"{node} declares no secrets block"
    token = secrets.get("GITHUB_TOKEN")
    assert isinstance(token, dict), f"{node} does not declare GITHUB_TOKEN"
    assert token.get("required") is True, (
        f"{node} must declare GITHUB_TOKEN required: true; read {token!r}"
    )
