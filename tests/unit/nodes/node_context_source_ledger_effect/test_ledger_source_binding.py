# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Nested context sources resolve explicit deploy tenants and deny unset ones."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from pydantic import ValidationError

from omnimarket.nodes.node_context_source_ledger_effect.handlers.handler_context_source_ledger import (
    HandlerContextSourceLedger,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    ModelLedgerSourceRow,
    load_ledger_source_binding,
)
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    HandlerContextSourceWorkState,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"


class _Reader:
    def __init__(self) -> None:
        self.reads = 0

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        self.reads += 1
        return ()


@pytest.mark.parametrize(
    ("handler_class", "factor"),
    [
        (HandlerContextSourceLedger, EnumContextFactor.OPERATOR_RULINGS),
        (HandlerContextSourceWorkState, EnumContextFactor.OPEN_WORK),
    ],
)
@pytest.mark.parametrize("tenant_overlay", [None, "", "  ", " lab "])
def test_pack_source_tenant_overlay_controls_reads(
    monkeypatch: pytest.MonkeyPatch,
    handler_class: type[HandlerContextSourceLedger]
    | type[HandlerContextSourceWorkState],
    factor: EnumContextFactor,
    tenant_overlay: str | None,
) -> None:
    if tenant_overlay is None:
        monkeypatch.delenv("ONEX_CONTEXT_SOURCE_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", tenant_overlay)
    reader = _Reader()
    handler = handler_class(reader=reader)
    request = ModelContextSourceRequest(
        factor=factor,
        scopes=("session:binding-proof",),
        revision="2026-10-23T12:00:00Z",
        access=ModelContextAccess(tenant_id="lab"),
    )
    configured = bool(tenant_overlay and tenant_overlay.strip())
    result = handler.handle(request)
    assert result.status is (
        EnumContextSourceStatus.UNRESOLVED
        if configured
        else EnumContextSourceStatus.DENIED
    )
    assert reader.reads == int(configured)
    other = request.model_copy(update={"access": ModelContextAccess(tenant_id="other")})
    assert handler.handle(other).status is EnumContextSourceStatus.DENIED
    assert reader.reads == int(configured)


@pytest.mark.parametrize("tenant_ids", ["lab", [23], [None], {"tenant": "lab"}])
def test_pack_source_tenant_overlay_rejects_malformed_declarations(
    tmp_path: Path, tenant_ids: object
) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "context_source_ledger": {
                    "ledger_source": {
                        "relation": "omninode_internal.work_ledger_rows",
                        "dsn_env": "OMNINODE_INTERNAL_DB_URL",
                        "binding_ref": "omninode_runtime_service",
                        "tenant_ids": tenant_ids,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="tenant_ids"):
        load_ledger_source_binding(path)


def test_pack_sources_share_one_declared_tenant_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_CONTEXT_SOURCE_TENANT_ID", "proof-tenant")
    for node in (
        "node_context_source_ledger_effect",
        "node_context_source_work_state_effect",
    ):
        assert load_ledger_source_binding(
            _NODES / node / "contract.yaml"
        ).tenant_ids == ("proof-tenant",)
