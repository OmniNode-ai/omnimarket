# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A principals overlay's roles fill a contract's ${NAME} placeholders (OMN-24106).

Seam: broker_acl (ModelNodeRole.placeholders, ModelBrokerAclPrincipalsOverlay.placeholder_logins) ->
node_broker_acl_reconcile_effect (role_logins, render_declaration in _apply_driver). A role that names a
placeholder supplies its login so no new environment variable is needed; a role that names a placeholder
holds exactly one principal; a placeholder is named by at most one role; roles are authoritative for the
names they declare and the environment still fills every other name.
Exemplar followed: test_readiness_check.py (overlay on tmp roots, FakeRemote broker, ENV from
test_aggregates_declared_grants.py).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.broker_acl import (
    ModelBrokerAclPrincipalsOverlay,
    ModelNodeRole,
)
from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    contract,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

GPU = "node_gpu_power_caps_effect"
PLACEHOLDER = "ONEX_ACL_GPU_POWER_CAPS_PRINCIPAL"
ROLE_LOGIN = "User:role-login"
ENV_LOGIN = "User:env-login"
TOPIC = "onex.cmd.omnimarket.gpu-power-caps-requested.v1"


def _env(
    tmp_path: Path,
    role: dict[str, Any] | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document: dict[str, Any] = {
        "scope": scope,
        "ticket": "OMN-24106",
        "why": "a test scope",
    }
    if role is not None:
        document["roles"] = [role]
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    env = {key: value for key, value in ENV.items() if key != PLACEHOLDER}
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    if extra:
        env.update(extra)
    return env


def _role(
    placeholders: tuple[str, ...] = (PLACEHOLDER,),
    principals: tuple[str, ...] = (ROLE_LOGIN,),
) -> dict[str, Any]:
    return {
        "name": "dev_runtime_effects",
        "role": "a unit on every lab host",
        "nodes": [GPU],
        "principals": list(principals),
        "placeholders": list(placeholders),
    }


def _apply(env: dict[str, str], remote: FakeRemote) -> Any:
    return HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode="apply")
    )


def test_role_login_fills_a_contract_placeholder(tmp_path: Path) -> None:
    """A role that names the contract's placeholder supplies its login: apply succeeds and the rows use it."""
    remote = FakeRemote()
    env = _env(tmp_path, _role())
    result = _apply(env, remote)
    assert result.exit_code == 0
    rows = [grant for grant in remote.live if grant.name == TOPIC]
    assert rows
    assert all(grant.principal == ROLE_LOGIN for grant in rows)
    assert not any("${" in grant.principal for grant in remote.live)


def test_unset_placeholder_with_no_role_is_refused_by_name(tmp_path: Path) -> None:
    """Failing control: the same contract with a role that names no placeholder is refused naming the unset name."""
    remote = FakeRemote()
    env = _env(tmp_path, _role(placeholders=()))
    result = _apply(env, remote)
    assert result.exit_code == 2
    assert PLACEHOLDER in result.error
    assert remote.live == set()


def test_role_wins_over_environment(tmp_path: Path) -> None:
    """The environment also sets the name: the role's login is used for that placeholder's rows only."""
    remote = FakeRemote()
    env = _env(tmp_path, _role(), extra={PLACEHOLDER: ENV_LOGIN})
    result = _apply(env, remote)
    assert result.exit_code == 0
    rows = [grant for grant in remote.live if grant.name == TOPIC]
    assert rows
    assert all(grant.principal == ROLE_LOGIN for grant in rows)
    assert not any(grant.principal == ENV_LOGIN for grant in rows)


def test_refused_role_with_two_logins_and_placeholder() -> None:
    """A role that names a placeholder and holds two logins is refused naming the count."""
    with pytest.raises(ValidationError) as error:
        ModelNodeRole(
            name="units",
            role="a unit",
            nodes=[GPU],
            principals=(ROLE_LOGIN, ENV_LOGIN),
            placeholders=(PLACEHOLDER,),
        )
    assert "2" in str(error.value)


def test_refused_placeholder_named_twice() -> None:
    """A placeholder named twice in one role, or by two roles, is refused naming the placeholder."""
    with pytest.raises(ValidationError) as one:
        ModelNodeRole(
            name="units",
            role="a unit",
            nodes=[GPU],
            principals=(ROLE_LOGIN,),
            placeholders=(PLACEHOLDER, PLACEHOLDER),
        )
    assert PLACEHOLDER in str(one.value)
    with pytest.raises(ValidationError) as two:
        ModelBrokerAclPrincipalsOverlay(
            scope="a scope",
            ticket="OMN-24106",
            why="a test scope",
            roles=(
                ModelNodeRole(
                    name="first",
                    role="a unit",
                    nodes=[GPU],
                    principals=(ROLE_LOGIN,),
                    placeholders=(PLACEHOLDER,),
                ),
                ModelNodeRole(
                    name="second",
                    role="a unit",
                    nodes=[GPU],
                    principals=(ENV_LOGIN,),
                    placeholders=(PLACEHOLDER,),
                ),
            ),
        )
    assert PLACEHOLDER in str(two.value)


def test_placeholder_logins_maps_each_name() -> None:
    """Two placeholders on one role both map to that role's single login."""
    overlay = ModelBrokerAclPrincipalsOverlay(
        scope="a scope",
        ticket="OMN-24106",
        why="a test scope",
        roles=(
            ModelNodeRole(
                name="units",
                role="a unit",
                nodes=[GPU],
                principals=(ROLE_LOGIN,),
                placeholders=("ONEX_ACL_A_PRINCIPAL", "ONEX_ACL_B_PRINCIPAL"),
            ),
        ),
    )
    assert overlay.placeholder_logins() == {
        "ONEX_ACL_A_PRINCIPAL": ROLE_LOGIN,
        "ONEX_ACL_B_PRINCIPAL": ROLE_LOGIN,
    }


def test_the_principals_example_names_a_role_placeholder_and_loads() -> None:
    """Seam: the shipped example overlay is a valid principals overlay and shows the placeholders key a real role uses."""
    text = (
        resources.files("omnimarket.nodes.node_broker_acl_overlay_render_compute")
        .joinpath("overlay.principals.example.yaml")
        .read_text(encoding="utf-8")
    )
    overlay = ModelBrokerAclPrincipalsOverlay.model_validate(yaml.safe_load(text))
    assert overlay.placeholder_logins() == {
        "ONEX_ACL_EXAMPLE_RUNTIME_PRINCIPAL": "User:example-runtime-login"
    }
