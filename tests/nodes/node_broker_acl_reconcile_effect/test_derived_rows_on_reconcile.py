# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconcile effect applies the rows derived from contract topics, additions only (OMN-19998, issue L1-04).

Seam: node_broker_acl_overlay_render_compute (derive_broker_acl_rows) -> node_broker_acl_reconcile_effect. The principals overlay is
read by scope; with no overlay nothing is derived, so a deployment that does not declare principals applies what it did before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    handler_broker_acl_reconcile as module,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    aggregate_declared_grants,
    contract,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

NODE = "node_broker_acl_overlay_render_compute"
COMMAND = "onex.cmd.omnimarket.broker-acl-overlay-render-requested.v1"


def _env(tmp_path: Path, principal: str = "User:derived-login") -> dict[str, str]:
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document = {
        "scope": scope,
        "ticket": "OMN-19998",
        "why": "a test scope",
        "principals": [{"principal": principal, "role": "a login", "nodes": [NODE]}],
    }
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {**ENV, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _handle(env: dict[str, str], remote: FakeRemote, mode: str):
    return HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode=mode)
    )


def test_apply_creates_the_derived_rows_and_reads_them_back(tmp_path: Path) -> None:
    remote = FakeRemote()
    result = _handle(_env(tmp_path), remote, "apply")
    assert result.exit_code == 0
    assert result.missing == 0
    assert result.derived is not None
    assert result.derived >= 6
    live = {(g.principal, g.resource_type, g.name, g.operation) for g in remote.live}
    assert ("User:derived-login", "TOPIC", COMMAND, "READ") in live
    assert (
        "User:derived-login",
        "GROUP",
        "local.omnimarket.node_broker_acl_overlay_render_compute.consume.v1.",
        "READ",
    ) in live
    assert all(op != "DELETE" for *_, op in live if _[0] == "User:derived-login"), (
        "additions only"
    )
    assert result.declared == len(aggregate_declared_grants()) + result.derived


def test_dry_run_creates_nothing_and_reports_the_derived_rows_missing(
    tmp_path: Path,
) -> None:
    remote = FakeRemote()
    result = _handle(_env(tmp_path), remote, "dry_run")
    assert not remote.live
    assert result.exit_code == 1
    assert result.missing == result.declared
    assert result.derived


def test_no_principals_overlay_derives_nothing_and_applies_what_it_did_before() -> None:
    remote = FakeRemote()
    result = _handle(ENV, remote, "dry_run")
    assert result.derived == 0
    assert result.uncovered_topics == 0
    assert result.declared == len(aggregate_declared_grants())


def test_a_derivation_the_apply_tool_would_refuse_stops_before_any_broker_call(
    tmp_path: Path,
) -> None:
    remote = FakeRemote()
    result = _handle(_env(tmp_path, principal="User:*"), remote, "apply")
    assert result.exit_code == 2
    assert "principal_invalid" in result.error
    assert remote.scripts == []


def test_injected_break_the_setting_comes_from_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Break: the contract names no derive scope; the overlay is present and still nothing is derived."""
    env = _env(tmp_path)
    assert _handle(env, FakeRemote(), "dry_run").derived
    loaded = contract()
    loaded["broker_acl_reconcile"] = {
        k: v
        for k, v in loaded["broker_acl_reconcile"].items()
        if k != "derive_principals_scope"
    }
    monkeypatch.setattr(module, "contract", lambda: loaded)
    assert _handle(env, FakeRemote(), "dry_run").derived == 0
