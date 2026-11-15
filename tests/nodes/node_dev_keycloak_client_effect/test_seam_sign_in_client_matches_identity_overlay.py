# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the client the dev probe user signs in with is the client whose tokens get the tenant mappers (OMN-24304).

The dev identity node signs the probe user in through the client named in its overlay (``user.client_id``);
the dev Keycloak client node adds the tenant claim mappers to the client named in its own overlay
(``sign_in_client.client_id``) and its contract lists that client as an allowed mapper client. If any of
the three names drifts, the probe user's token carries no tenant and the signed-in read answers 401.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
KEYCLOAK_NODE = NODES / "node_dev_keycloak_client_effect"
IDENTITY_NODE = NODES / "node_dev_identity_provision_effect"


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def _problems(
    identity: dict[str, Any], keycloak: dict[str, Any], contract: dict[str, Any]
) -> list[str]:
    found: list[str] = []
    signs_in_with = identity["user"]["client_id"]
    if keycloak["sign_in_client"]["client_id"] != signs_in_with:
        found.append("the mappers are on a client the probe user does not sign in with")
    if signs_in_with not in contract["dev_only"]["mapper_clients"]:
        found.append("the contract does not allow mappers on the sign-in client")
    return found


def test_the_identity_sign_in_client_is_the_mapper_client_in_both_overlay_and_contract() -> (
    None
):
    identity = _load(IDENTITY_NODE / "overlay.example.yaml")
    keycloak = _load(KEYCLOAK_NODE / "overlay.example.yaml")
    contract = _load(KEYCLOAK_NODE / "contract.yaml")

    assert _problems(identity, keycloak, contract) == []


def test_failing_control_a_renamed_client_on_either_side_breaks_the_seam() -> None:
    identity = _load(IDENTITY_NODE / "overlay.example.yaml")
    keycloak = _load(KEYCLOAK_NODE / "overlay.example.yaml")
    contract = _load(KEYCLOAK_NODE / "contract.yaml")

    renamed_identity = {**identity, "user": {**identity["user"], "client_id": "other"}}
    assert _problems(renamed_identity, keycloak, contract)

    renamed_keycloak = {
        **keycloak,
        "sign_in_client": {**keycloak["sign_in_client"], "client_id": "other"},
    }
    assert _problems(identity, renamed_keycloak, contract)

    narrowed = {**contract, "dev_only": {**contract["dev_only"], "mapper_clients": []}}
    assert _problems(identity, keycloak, narrowed)
