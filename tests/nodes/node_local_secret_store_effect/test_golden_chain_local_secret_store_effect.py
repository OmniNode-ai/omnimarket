# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_local_secret_store_effect (OMN-19985).

The chain the Credentials page reads, end to end on a local store, through the
same read the local dashboard serves:

    onex secret set -> EFFECT store write -> credential-registered
                    -> in-process fold -> tenant_inference_credentials
                    -> tenant-credentials.v1 read for the local tenant
                       (ref, provider, fingerprint prefix, set time; no value)
    onex secret delete -> credential-revoked -> the same read shows it revoked

It fails if any link drops the key off the page, if the served read names the
value, or if the delete removes the row instead of revoking it.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_INSTALL_TENANT_ID,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.read_page import pagination_order_spec

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_PLANTED = "sk-or-v1-planted-omn19985-golden-0123456789"
_TOPIC = "onex.snapshot.projection.tenant-credentials.v1"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_tenant_credentials/contract.yaml"
)


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        "omnimarket.cli.cli_secret._resolve_model", lambda *_args, **_kw: None
    )
    return db_path


def _served(db_path: Path) -> list[dict[str, Any]]:
    """tenant-credentials.v1 for the local tenant, read the way the dashboard serves it."""
    [cfg] = [
        exposure
        for exposure in load_projection_exposures_from_contract(
            yaml.safe_load(_CONTRACT.read_text()),
            "node_projection_tenant_credentials",
            _CONTRACT,
        )
        if exposure.topic == _TOPIC
    ]
    return asyncio.run(
        SqliteTableRowSource(db_path).rows(
            cfg,
            order_spec=pagination_order_spec(cfg, cfg.order_by_spec),
            tenant_id=LOCAL_INSTALL_TENANT_ID,
        )
    )


def test_golden_chain_set_then_delete_reaches_the_served_exposure(store: Path) -> None:
    runner = CliRunner()
    set_result = runner.invoke(
        secret_group, ["set", _REF], input=_PLANTED, catch_exceptions=False
    )
    assert set_result.exit_code == 0, set_result.output

    [row] = _served(store)
    assert row["name"] == _REF
    assert row["provider"] == "openrouter"
    assert row["fingerprint"] == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]
    assert row["set_at"] is not None
    assert row["revoked_at"] is None
    assert str(row["api_key_ref"]).startswith("cred_localinstall_openrouter_")
    assert _PLANTED not in repr(row)

    delete_result = runner.invoke(
        secret_group, ["delete", _REF], catch_exceptions=False
    )
    assert delete_result.exit_code == 0, delete_result.output

    [row] = _served(store)
    assert row["revoked_at"] is not None
    assert row["fingerprint"] == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]
    assert _PLANTED not in repr(row)
