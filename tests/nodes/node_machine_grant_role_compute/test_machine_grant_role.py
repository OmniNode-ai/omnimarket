# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scm-writer binding comes from the overlay; its grants come from the contract (OMN-22139)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_github_repo_write_effect as write_node
import omnimarket.nodes.node_machine_grant_role_compute as node_package
from omnimarket.nodes.node_machine_grant_role_compute import HandlerMachineGrantRole
from omnimarket.nodes.node_machine_grant_role_compute.handlers import (
    declared_grant_roles,
)
from omnimarket.nodes.node_machine_grant_role_compute.models import (
    ModelMachineGrantRoleRequest,
)
from tests.nodes.node_machine_grant_role_compute.overlay_fixture import (
    TWO_WRITERS,
    developer,
    machine,
    overlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def _handle(**kwargs: object) -> dict[str, object]:
    request = ModelMachineGrantRoleRequest.model_validate(kwargs)
    return HandlerMachineGrantRole().handle(request).model_dump(mode="json")


def test_scm_writer_grants_are_the_write_node_dispatch_topics() -> None:
    """The grant set is pinned to the topics node_github_repo_write_effect really uses."""
    write = yaml.safe_load(
        (Path(write_node.__file__).parent / "contract.yaml").read_text()
    )
    dispatch = write["runtime_dispatch"]
    grants = {
        (g.operation, g.resource_name) for g in declared_grant_roles()["scm-writer"]
    }
    assert ("write", dispatch["command_topic"]) in grants
    for topic in dispatch["terminal_events"].values():
        assert ("read", topic) in grants
        assert ("describe", topic) in grants
    assert {topic for _, topic in grants} == {
        dispatch["command_topic"],
        *dispatch["terminal_events"].values(),
    }


def test_machines_bound_in_the_overlay_get_the_role_and_nothing_else_does() -> None:
    result = _handle(overlay=TWO_WRITERS)
    assert result["status"] == "resolved"
    assert result["declared_roles"] == ["scm-writer"]
    holders = [(b["principal"], b["role"]) for b in result["bindings"]]
    assert holders == [("lab-host-a", "scm-writer"), ("lab-host-b", "scm-writer")]
    assert result["bindings"][0]["acl_principal"] == "User:lab-host-a"
    assert {g["operation"] for g in result["bindings"][0]["grants"]} == {
        "write",
        "read",
        "describe",
    }


def test_an_overlay_with_no_binding_grants_nothing() -> None:
    """Negative control: the same node with the rows unbound resolves no binding."""
    result = _handle(overlay=overlay(machine("lab-host-a"), developer("workstation-a")))
    assert result["status"] == "resolved"
    assert result["bindings"] == []


def test_principal_and_role_narrow_the_answer() -> None:
    result = _handle(overlay=TWO_WRITERS, principal="lab-host-b", role="scm-writer")
    assert [b["principal"] for b in result["bindings"]] == ["lab-host-b"]
    assert _handle(overlay=TWO_WRITERS, role="other-role")["bindings"] == []


def test_an_undeclared_role_is_refused_not_ignored() -> None:
    result = _handle(overlay=overlay(machine("lab-host-a", "scm-writer", "root")))
    assert result["status"] == "refused"
    assert result["refusal"] == "grant-role-undeclared"
    assert "principals.0" in result["detail"]
    assert result["bindings"] == []


def test_a_developer_row_cannot_bind_a_role() -> None:
    row = developer("workstation-a") | {"grant_roles": ["scm-writer"]}
    result = _handle(overlay=overlay(row))
    assert result["status"] == "refused"
    assert result["refusal"] == "overlay-invalid"


def test_the_overlay_is_read_from_the_overlay_roots(tmp_path: Path) -> None:
    target = tmp_path / "developer.principal" / "overlay.yaml"
    target.parent.mkdir()
    target.write_text(yaml.safe_dump(TWO_WRITERS), encoding="utf-8")
    result = _handle(overlay_roots=[str(tmp_path)])
    assert [b["principal"] for b in result["bindings"]] == ["lab-host-a", "lab-host-b"]


def test_the_overlay_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "developer.principal" / "overlay.yaml"
    target.parent.mkdir()
    target.write_text(yaml.safe_dump(TWO_WRITERS), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert len(_handle()["bindings"]) == 2


def test_no_overlay_is_refused(tmp_path: Path) -> None:
    result = _handle(overlay_roots=[str(tmp_path)])
    assert result["status"] == "refused"
    assert result["refusal"] == "overlay-missing"


def test_node_code_names_no_principal() -> None:
    """Every binding is overlay data: the node's files carry no principal of the fixture."""
    text = "".join(
        path.read_text(encoding="utf-8")
        for path in NODE_DIR.rglob("*")
        if path.suffix in {".py", ".yaml"}
    )
    for name in ("lab-host-a", "lab-host-b"):
        assert name not in text
