# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real canonical dashboard lifecycle; opt in to fetching its pinned bundle.

Failure modes: fake readiness, auth omitted, duplicate server on replay, leaked
session token, wrong tenant, and signalling an unowned PID while stopping.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import mint_local_tenant_identity
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_dashboard import (
    HandlerSetupDashboard,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.e2e


def test_actual_dashboard_is_authenticated_idempotent_and_tenant_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("ONEX_DASHBOARD_REAL") != "1":
        pytest.skip("Explicit real dashboard proof requires ONEX_DASHBOARD_REAL=1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    for key in (
        "OMNI_HOME",
        "OMNIBASE_PATH",
        "ONEX_CONTRACTS_DIR",
        "DELEGATION_ROUTING_OVERLAY_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    store = LocalByokCredentialStore(
        db_path=home / ".omninode" / "delegation" / "delegation.sqlite"
    )
    assert store.db_path.is_relative_to(home)
    identity = mint_local_tenant_identity(db_path=store.db_path)
    handler = HandlerSetupDashboard(onex_home=home / ".onex", store=store)
    plan = ModelSetupPlan(workspace=tmp_path / "workspace", components=("dashboard",))
    assert handler.observe(plan).status == "unavailable"
    try:
        handler.apply(plan)
        first = handler.observe(plan)
        assert first.status == "success", first.model_dump_json()
        url = first.evidence["url"]
        token = asyncio.run(store.get_secret(first.evidence["credential_ref"]))
        assert token
        with httpx.Client(trust_env=False) as client:
            assert client.get(url + "/projections").status_code == 401
            response = client.get(
                url + "/projections", headers={"authorization": "Bearer " + token}
            )
            assert response.status_code == 200
            assert response.json()["tenant"] == str(identity.tenant_uuid)
            assert (
                client.get(
                    url + "/", headers={"authorization": "Bearer " + token}
                ).status_code
                == 200
            )
        metadata = home / ".onex" / "dashboard.json"
        before = metadata.read_bytes()
        handler.apply(plan)
        assert metadata.read_bytes() == before
        assert handler.observe(plan).evidence["pid"] == first.evidence["pid"]
        assert token not in metadata.read_text()
        assert token not in first.model_dump_json()
        proof = {
            "url": url,
            "tenant_id": str(identity.tenant_uuid),
            "pid": first.evidence["pid"],
            "authentication_required": True,
            "idempotent": True,
        }
        export = os.environ.get("ONEX_REAL_CLAUDE_EVIDENCE")
        if export:
            target = Path(export)
            target.mkdir(parents=True, exist_ok=True)
            (target / "dashboard-lifecycle.json").write_text(
                json.dumps(proof, indent=2)
            )
    finally:
        handler.stop()
    assert handler.observe(plan).status == "unavailable"
