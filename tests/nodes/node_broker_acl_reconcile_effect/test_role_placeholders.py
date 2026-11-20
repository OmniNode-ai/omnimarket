# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A principals overlay's roles fill a contract's ${NAME} placeholders (OMN-24106).

Seam: broker_acl (ModelNodeRole.placeholders, ModelBrokerAclPrincipalsOverlay.placeholder_logins) ->
node_broker_acl_reconcile_effect (fill_source, render_declaration in _apply_driver). A role that names a
placeholder supplies its login so no new environment variable is needed; a role that names a placeholder
holds exactly one principal; a placeholder is named by at most one role; when an overlay is found it is the
one source: the environment fills no name.
Exemplar followed: test_readiness_check.py (overlay on tmp roots, FakeRemote broker, ENV from
test_aggregates_declared_grants.py).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
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
    demanded_placeholders,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

NODE = "node_branch_deploy_orchestrator"
PLACEHOLDER = "ONEX_ACL_BRANCH_DEPLOY_PRINCIPAL"
ROLE_LOGIN = "User:role-login"
ENV_LOGIN = "User:env-login"
TOPIC = "onex.cmd.omnimarket.branch-deploy-command-requested.v1"
FLEET_PACKAGE = "ONEX_ACL_FLEET_PACKAGE"
FLEET_GROUP = "local.fleetpkg.host_drift_fleet.h"


def _fill_rest(taken: set[str]) -> dict[str, str]:
    """placeholder_values for every other name the node demands, so a test overlay fills the whole contract set (the overlay is the one source)."""
    return {
        name: "fleetpkg" if name == FLEET_PACKAGE else "User:filler"
        for name in sorted(demanded_placeholders())
        if name not in taken
    }


def _env(
    tmp_path: Path,
    role: dict[str, Any] | None,
    extra: dict[str, str] | None = None,
    omit: tuple[str, ...] = (),
) -> dict[str, str]:
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document: dict[str, Any] = {
        "scope": scope,
        "ticket": "OMN-24106",
        "why": "a test scope",
    }
    taken = set(omit)
    if role is not None:
        document["roles"] = [role]
        taken |= set(role["placeholders"])
    document["placeholder_values"] = _fill_rest(taken)
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
        "nodes": [NODE],
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
    env = _env(tmp_path, _role(placeholders=()), omit=(PLACEHOLDER,))
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
            nodes=[NODE],
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
            nodes=[NODE],
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
                    nodes=[NODE],
                    principals=(ROLE_LOGIN,),
                    placeholders=(PLACEHOLDER,),
                ),
                ModelNodeRole(
                    name="second",
                    role="a unit",
                    nodes=[NODE],
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
                nodes=[NODE],
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


def _env_with_values(
    tmp_path: Path, values: dict[str, str], omit: tuple[str, ...] = ()
) -> dict[str, str]:
    """`_env` for an overlay that also carries placeholder_values: its role names no placeholder, so these fill names; every other demanded name is filled too except those in omit."""
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document: dict[str, Any] = {
        "scope": scope,
        "ticket": "OMN-24106",
        "why": "a test scope",
        "roles": [_role(placeholders=())],
        "placeholder_values": {
            **_fill_rest(set(values) | set(omit)),
            **values,
        },
    }
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    env = {key: value for key, value in ENV.items() if key != FLEET_PACKAGE}
    env["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path)
    return env


def test_placeholder_value_fills_a_contract_placeholder(tmp_path: Path) -> None:
    """The overlay's placeholder_values fill the contract's fleet package name: apply succeeds using it."""
    remote = FakeRemote()
    env = _env_with_values(tmp_path, {FLEET_PACKAGE: "fleetpkg"})
    result = _apply(env, remote)
    assert result.exit_code == 0
    rows = [grant for grant in remote.live if grant.name == FLEET_GROUP]
    assert rows
    assert all(grant.resource_type == "GROUP" for grant in rows)
    assert not any("${" in grant.name for grant in remote.live)


def test_missing_placeholder_value_is_refused_by_name(tmp_path: Path) -> None:
    """Failing control: no placeholder_values and no environment variable is refused naming the name."""
    remote = FakeRemote()
    env = _env_with_values(tmp_path, {}, omit=(FLEET_PACKAGE,))
    result = _apply(env, remote)
    assert result.exit_code == 2
    assert FLEET_PACKAGE in result.error
    assert remote.live == set()


def test_placeholder_value_named_by_a_role_is_refused() -> None:
    """A placeholder_values name a role also names, a lowercase name and an empty value are refused naming the name."""
    with pytest.raises(ValidationError, match="named by both") as conflict:
        ModelBrokerAclPrincipalsOverlay(
            scope="a scope",
            ticket="OMN-24106",
            why="a test scope",
            roles=(
                ModelNodeRole(
                    name="units",
                    role="a unit",
                    nodes=[NODE],
                    principals=(ROLE_LOGIN,),
                    placeholders=(PLACEHOLDER,),
                ),
            ),
            placeholder_values={PLACEHOLDER: "x"},
        )
    assert PLACEHOLDER in str(conflict.value)
    quiet = ModelNodeRole(
        name="units", role="a unit", nodes=[NODE], principals=(ROLE_LOGIN,)
    )
    with pytest.raises(ValidationError, match="does not match") as lowercase:
        ModelBrokerAclPrincipalsOverlay(
            scope="a scope",
            ticket="OMN-24106",
            why="a test scope",
            roles=(quiet,),
            placeholder_values={"onex_acl_a": "x"},
        )
    assert "onex_acl_a" in str(lowercase.value)
    with pytest.raises(ValidationError, match="empty or blank") as empty:
        ModelBrokerAclPrincipalsOverlay(
            scope="a scope",
            ticket="OMN-24106",
            why="a test scope",
            roles=(quiet,),
            placeholder_values={"ONEX_ACL_A_PRINCIPAL": ""},
        )
    assert "ONEX_ACL_A_PRINCIPAL" in str(empty.value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "empty or blank"),
        ("   ", "empty or blank"),
        ("${ONEX_ACL_OTHER}", "unexpanded placeholder"),
        ("$HOME", "unexpanded placeholder"),
        ("a b", "whitespace"),
    ],
)
def test_placeholder_value_refused_by_reason(value: str, reason: str) -> None:
    """A placeholder value with a bad shape is refused naming the name and the reason."""
    quiet = ModelNodeRole(
        name="units", role="a unit", nodes=[NODE], principals=(ROLE_LOGIN,)
    )
    with pytest.raises(ValidationError) as refused:
        ModelBrokerAclPrincipalsOverlay(
            scope="a scope",
            ticket="OMN-24106",
            why="a test scope",
            roles=(quiet,),
            placeholder_values={"ONEX_ACL_A_PRINCIPAL": value},
        )
    assert "ONEX_ACL_A_PRINCIPAL" in str(refused.value)
    assert reason in str(refused.value)


def test_role_principal_refused_when_it_fills_a_placeholder() -> None:
    """A role that fills a placeholder with a bad principal is refused; a role without placeholders is not."""
    with pytest.raises(ValidationError, match="principal has"):
        ModelNodeRole(
            name="units",
            role="a unit",
            nodes=[NODE],
            principals=("",),
            placeholders=(PLACEHOLDER,),
        )
    with pytest.raises(ValidationError, match="principal has") as unexpanded:
        ModelNodeRole(
            name="units",
            role="a unit",
            nodes=[NODE],
            principals=("${X}",),
            placeholders=(PLACEHOLDER,),
        )
    assert "principal has" in str(unexpanded.value)
    accepted = ModelNodeRole(
        name="units", role="a unit", nodes=[NODE], principals=("",)
    )
    assert accepted.principals == ("",)


def test_environment_does_not_fill_a_name_the_overlay_lacks(tmp_path: Path) -> None:
    """Fails on the base because the environment filled the name there: the overlay is the one source."""
    remote = FakeRemote()
    env = _env(
        tmp_path,
        _role(placeholders=()),
        extra={PLACEHOLDER: ENV_LOGIN},
        omit=(PLACEHOLDER,),
    )
    result = _apply(env, remote)
    assert result.exit_code == 2
    assert PLACEHOLDER in result.error
    assert remote.live == set()


def _recording(calls: list[list[str]], texts: list[str]):
    def run(argv: Sequence[str], timeout: float):
        calls.append(list(argv))
        declaration_path = Path(argv[argv.index("--declaration") + 1])
        texts.append(declaration_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_legacy_command_path_uses_the_overlay_fills(tmp_path: Path) -> None:
    """The legacy command path renders the declaration from the overlay's fills, not the environment."""
    calls: list[list[str]] = []
    texts: list[str] = []
    env = _env_with_values(
        tmp_path,
        {
            FLEET_PACKAGE: "fleetpkg",
            "ONEX_ACL_GUARD_PRINCIPAL": "User:guard-from-overlay",
        },
    )
    env["ONEX_ACL_RECONCILE_COMMAND"] = "reconcile-cmd --flag"
    env["ONEX_LAB_BROKER_SSH_TARGET"] = "broker"
    handler = HandlerBrokerAclReconcile(_recording(calls, texts), env)
    result = handler.handle(ModelBrokerAclReconcileRequest(mode="dry_run"))
    assert result.exit_code != 2
    assert len(calls) == 1
    text = texts[0]
    assert "User:guard-from-overlay" in text
    assert "fleetpkg" in text
    assert "${" not in text


def test_legacy_command_path_ignores_the_environment_when_an_overlay_is_found(
    tmp_path: Path,
) -> None:
    """Failing control: the overlay lacks a name the environment sets: the environment is not consulted."""
    calls: list[list[str]] = []
    texts: list[str] = []
    env = _env_with_values(
        tmp_path,
        {"ONEX_ACL_GUARD_PRINCIPAL": "User:guard"},
        omit=(FLEET_PACKAGE,),
    )
    env["ONEX_ACL_RECONCILE_COMMAND"] = "reconcile-cmd --flag"
    env["ONEX_LAB_BROKER_SSH_TARGET"] = "broker"
    env[FLEET_PACKAGE] = "env-fleetpkg"
    handler = HandlerBrokerAclReconcile(_recording(calls, texts), env)
    result = handler.handle(ModelBrokerAclReconcileRequest(mode="dry_run"))
    assert result.exit_code == 2
    assert FLEET_PACKAGE in result.error
    assert calls == []


def test_placeholder_fills_merges_logins_and_values() -> None:
    """placeholder_fills maps role placeholders to the role's login and placeholder_values to their values."""
    overlay = ModelBrokerAclPrincipalsOverlay(
        scope="a scope",
        ticket="OMN-24106",
        why="a test scope",
        roles=(
            ModelNodeRole(
                name="units",
                role="a unit",
                nodes=[NODE],
                principals=(ROLE_LOGIN,),
                placeholders=("ONEX_ACL_A_PRINCIPAL",),
            ),
        ),
        placeholder_values={"ONEX_ACL_B_PRINCIPAL": "fleetpkg"},
    )
    assert overlay.placeholder_fills() == {
        "ONEX_ACL_A_PRINCIPAL": ROLE_LOGIN,
        "ONEX_ACL_B_PRINCIPAL": "fleetpkg",
    }
