# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two tenant read bus handlers build a Postgres reader from a resolver.

The runtime builds both bus handlers with no constructor arguments, so their
``reader`` is ``None`` and every read answered ``not_configured``. Now the handler
accepts a ``secret_resolver`` the runtime hands it, and -- when it is not handed
one -- reads the lane's rendered resolver config to build the reader. A missing
resolver still answers the typed ``not_configured`` terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.handlers.tenant_projection_read.analytics_postgres import (
    Psycopg2AnalyticsQueryExecutor,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
    ModelTenantReadFailed,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.handler_tenant_delegations_read_bus import (
    HandlerTenantDelegationsReadBus,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read_bus import (
    HandlerTenantSavingsReadBus,
)

pytestmark = pytest.mark.unit

TENANT = uuid4()
SECRET_REF = "omnidash.analytics.db_url"
FAKE_DSN = "postgresql://fake"


class FakeResolver:
    """Answers a DSN for every logical name and records the names it is asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get_secret_async(
        self, logical_name: str, required: bool = True
    ) -> SecretStr | None:
        self.asked.append(logical_name)
        return SecretStr(FAKE_DSN)


def _command(kind: EnumTenantReadKind) -> ModelTenantReadCommand:
    return ModelTenantReadCommand(correlation_id=uuid4(), tenant_id=TENANT, kind=kind)


def _patch_run(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    """The Postgres runner answers from the rows, never opening a connection."""

    def run(*_args: object) -> list[dict]:
        return list(rows)

    monkeypatch.setattr(Psycopg2AnalyticsQueryExecutor, "_run", staticmethod(run))


def _write_config(path, mappings: list[dict]) -> None:
    path.write_text(
        yaml.safe_dump(
            {"enable_convention_fallback": False, "mappings": mappings},
        ),
        encoding="utf-8",
    )


_SAVINGS_ROW = {
    "total_saved_usd": 4.0,
    "total_spend_usd": 2.0,
    "delegation_runs": 3,
    "total_tokens": 1200,
    "savings_latest": datetime(2026, 1, 1, tzinfo=UTC),
    "runs_latest": datetime(2026, 1, 2, tzinfo=UTC),
}
_DELEGATIONS_ROW = {
    "run_id": "run-a",
    "task_type": "t",
    "model": "m",
    "quality_gate_passed": True,
    "cost_usd": 1.0,
    "saved_usd": 2.0,
    "tokens": 500,
    "data_source": "real",
    "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
}

HANDLERS = [
    pytest.param(
        HandlerTenantSavingsReadBus,
        EnumTenantReadKind.SAVINGS,
        _SAVINGS_ROW,
        id="savings",
    ),
    pytest.param(
        HandlerTenantDelegationsReadBus,
        EnumTenantReadKind.DELEGATIONS,
        _DELEGATIONS_ROW,
        id="delegations",
    ),
]


@pytest.mark.parametrize(("handler_cls", "kind", "row"), HANDLERS)
async def test_injected_resolver_builds_reader(
    handler_cls, kind, row, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver the runtime hands the handler resolves the DSN and the read completes."""
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    resolver = FakeResolver()
    _patch_run(monkeypatch, [row])
    answer = await handler_cls(secret_resolver=resolver).handle(_command(kind))
    assert isinstance(answer, ModelTenantReadCompleted), answer
    assert SECRET_REF in resolver.asked, resolver.asked


@pytest.mark.parametrize(("handler_cls", "kind", "row"), HANDLERS)
async def test_no_resolver_and_no_config_path_answers_not_configured(
    handler_cls, kind, row, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No resolver and no rendered config: the typed not_configured terminal, no raise."""
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    answer = await handler_cls().handle(_command(kind))
    assert isinstance(answer, ModelTenantReadFailed), answer
    assert answer.reason == "not_configured"


@pytest.mark.parametrize(("handler_cls", "kind", "row"), HANDLERS)
async def test_runtime_config_path_builds_reader(
    handler_cls, kind, row, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With no injection the handler reads the lane's rendered resolver config."""
    config = tmp_path / "secret_resolver.yaml"
    _write_config(
        config,
        [
            {
                "logical_name": SECRET_REF,
                "source": {
                    "source_type": "env",
                    "source_path": "OMNIDASH_ANALYTICS_DB_URL",
                },
            }
        ],
    )
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config))
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", FAKE_DSN)
    _patch_run(monkeypatch, [row])
    answer = await handler_cls().handle(_command(kind))
    assert isinstance(answer, ModelTenantReadCompleted), answer


@pytest.mark.parametrize(("handler_cls", "kind", "row"), HANDLERS)
async def test_config_without_the_secret_answers_not_configured(
    handler_cls, kind, row, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A rendered config that does not map the secret leaves the reader unbound."""
    config = tmp_path / "secret_resolver.yaml"
    _write_config(config, [])
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config))
    answer = await handler_cls().handle(_command(kind))
    assert isinstance(answer, ModelTenantReadFailed), answer
    assert answer.reason == "not_configured"


@pytest.mark.parametrize(("handler_cls", "kind", "row"), HANDLERS)
async def test_unreadable_config_path_answers_not_configured(
    handler_cls, kind, row, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A config path that cannot be read is not_configured, not a raised error."""
    monkeypatch.setenv(
        "ONEX_SECRET_RESOLVER_CONFIG_PATH", str(tmp_path / "does_not_exist.yaml")
    )
    answer = await handler_cls().handle(_command(kind))
    assert isinstance(answer, ModelTenantReadFailed), answer
    assert answer.reason == "not_configured"


@pytest.mark.parametrize(
    "package",
    [
        "omnimarket.nodes.node_tenant_savings_read_effect",
        "omnimarket.nodes.node_tenant_delegations_read_effect",
    ],
)
def test_seam_contract_secret_name_is_the_runtime_mapping_name(package: str) -> None:
    """Seam with the runtime's resolver mapping: both contracts name the logical secret the dev profile maps.

    The name is spelled here because the mapping lives in another repository
    (omnibase_infra runtime_policy dev profile, omnibase_infra#5903); a contract
    that renames it, or a mapping that does, turns every read not_configured.
    """
    assert (
        packaged_contract_value(package, "config", "analytics_dsn_secret") == SECRET_REF
    )
