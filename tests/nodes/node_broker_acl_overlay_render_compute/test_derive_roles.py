# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A role: one node set run under several logins at once, so a unit on every lab host holds its rows on each (OMN-24005, plan M5).

Seam: the principals overlay's roles (overlays repository, scope broker_acl_principals) -> HandlerBrokerAclDerive. The 5926 model
held one login per node, so a node run on six hosts refused with node_principal_conflict; a role is the shape that says it.
Exemplar followed: test_derive_chain.py (fixture contracts, the same request builder).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.broker_acl import (
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclPrincipalsOverlay,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)

pytestmark = pytest.mark.unit

_CODES = EnumBrokerAclFindingCode
CMD = "onex.cmd.omnimarket.alpha-requested.v1"
EVT = "onex.evt.omnimarket.alpha-completed.v1"
HOSTS = ("User:host-a-login", "User:host-b-login", "User:host-c-login")


def _fixture(root: Path) -> None:
    target = root / "node_alpha_effect"
    target.mkdir(parents=True)
    document = {
        "name": "node_alpha_effect",
        "event_bus": {"subscribe_topics": [CMD], "publish_topics": [EVT]},
    }
    (target / "contract.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _overlay(**sections: object) -> ModelBrokerAclPrincipalsOverlay:
    return ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-24005",
            "why": "a test",
            **sections,
        }
    )


def _derive(
    root: Path, overlay: ModelBrokerAclPrincipalsOverlay, only_node: str | None = None
):
    return HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope="broker_acl_principals",
            topic_uses=load_topic_uses(root),
            principals=overlay,
            only_node=only_node,
        )
    )


def _role(nodes: list[str], principals: tuple[str, ...] = HOSTS) -> dict[str, object]:
    return {
        "name": "units",
        "role": "a unit on every host",
        "nodes": nodes,
        "principals": list(principals),
    }


def test_a_role_derives_the_rows_of_its_nodes_for_every_member(tmp_path: Path) -> None:
    """Three logins in a role each hold the node's topic and group rows; no node_principal_conflict is raised."""
    _fixture(tmp_path)
    result = _derive(tmp_path, _overlay(roles=[_role(["node_alpha_effect"])]))
    assert result.status == "derived"
    assert result.findings == ()
    for login in HOSTS:
        mine = {
            (r.resource_type.value, r.name, r.operation.value)
            for r in result.rows
            if r.principal == login
        }
        assert ("TOPIC", CMD, "READ") in mine
        assert ("TOPIC", EVT, "WRITE") in mine
    assert len(result.rows) == len(HOSTS) * len(
        {r for r in result.rows if r.principal == HOSTS[0]}
    )


def test_a_node_in_two_exclusive_entries_still_conflicts(tmp_path: Path) -> None:
    """Failing control: the exclusive principals list keeps its conflict refusal; only a role admits many logins."""
    _fixture(tmp_path)
    overlay = _overlay(
        principals=[
            {"principal": "User:a-login", "role": "x", "nodes": ["node_alpha_effect"]},
            {"principal": "User:b-login", "role": "x", "nodes": ["node_alpha_effect"]},
        ]
    )
    assert _CODES.NODE_PRINCIPAL_CONFLICT in {
        f.code for f in _derive(tmp_path, overlay).findings
    }


def test_role_members_and_nodes_are_checked(tmp_path: Path) -> None:
    """A wildcard member and a node with no contract are refused with their finding codes and derive no rows."""
    _fixture(tmp_path)
    result = _derive(
        tmp_path,
        _overlay(
            roles=[
                _role(
                    ["node_alpha_effect", "node_ghost_effect"],
                    ("User:*", "User:ok-login"),
                )
            ]
        ),
    )
    codes = {f.code for f in result.findings}
    assert {_CODES.PRINCIPAL_INVALID, _CODES.NODE_WITHOUT_CONTRACT} <= codes
    assert result.rows == ()


def test_only_node_derives_one_nodes_rows_and_refuses_an_unmapped_node(
    tmp_path: Path,
) -> None:
    """The readiness check's question: one node's rows for its members; a node no principal runs refuses."""
    _fixture(tmp_path)
    overlay = _overlay(roles=[_role(["node_alpha_effect"])])
    assert {
        r.principal for r in _derive(tmp_path, overlay, "node_alpha_effect").rows
    } == set(HOSTS)
    refused = _derive(tmp_path, overlay, "node_other_effect")
    assert [f.code for f in refused.findings if f.code == _CODES.NODE_WITHOUT_PRINCIPAL]
    assert refused.rows == ()


def test_an_overlay_names_a_principal_or_a_role() -> None:
    """An overlay with neither is refused by the model; with only roles it is valid."""
    with pytest.raises(ValueError, match="at least one principal or role"):
        _overlay()
    assert _overlay(roles=[_role(["node_alpha_effect"])]).roles
