# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Own-key (tenant overlay) cost is not platform spend: the ceiling readers skip it (OMN-24874).

The delegation terminal books a tenant overlay run ``metered`` with a cost above
zero (the customer's own key paid it). The readers that sum cost into the
platform's per-tenant monthly ceiling, ``materialize_budget_state`` and its async
twin, resolve the tier's cost model through ``resolve_tier_cost``, and the routing
registry declares no ``tenant_overlay`` tier, so they have always skipped it by
accident of the registry. ``resolve_tier_cost`` now returns None for the tenant
overlay tier whatever the registry declares, so each reader reads the tier as not
budgeted (``skipped_reason`` ``tier_not_budgeted``, the async twin returns before
any statement). These tests patch the registry with a capped ``tenant_overlay``
tier: without that rule it would draw the customer's own spend down against the
platform ceiling.
"""

from __future__ import annotations

import ast
import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_core.models.delegation.wire import EnumTierCostType, ModelTierCost

from omnimarket.models.model_tenant_routing_overlay_backend import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    TABLE,
    ModelDelegationBudgetStateEvent,
    materialize_budget_state,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.pricing import recompute_actual_cost_and_savings, resolve_tier_cost
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_LOAD_REGISTRY = "omnimarket.pricing._load_routing_config"
# A registry that (wrongly) declares a monthly cap for every tier name it is asked about, the case the rule in resolve_tier_cost guards.
_CAPPED = ModelTierCost(
    cost_type=EnumTierCostType.BUDGETED,
    rate_per_1k_usd=0.01,
    monthly_cap_usd=100.0,
    overage_rate_per_1k_usd=0.02,
)
_OWN_KEY_COST = Decimal("0.000145")
_REGISTRY = SimpleNamespace(
    tiers=[
        SimpleNamespace(name=name, cost=_CAPPED)
        for name in (TENANT_OVERLAY_TIER_NAME, "cheap_cloud", "ceiling-budgeted")
    ]
)


def _event(tier: str) -> ModelDelegationBudgetStateEvent:
    return ModelDelegationBudgetStateEvent(
        correlation_id="omn24874-own-key-run",
        cost_tier_name=tier,
        cost_measurement_source="metered",
        budget_headroom_consumed_usd=Decimal("0"),
        cost_usd=_OWN_KEY_COST,
        tenant_id="readiness-probe",
        timestamp="2026-11-16T18:00:00+00:00",
    )


@pytest.mark.unit
def test_tenant_overlay_run_never_draws_the_platform_ceiling_budget_state() -> None:
    db = InmemoryDatabaseAdapter()
    with patch(_LOAD_REGISTRY, return_value=_REGISTRY):
        result = materialize_budget_state(_event(TENANT_OVERLAY_TIER_NAME), db)
    assert result.rows_upserted == 0
    assert result.skipped_reason == "tier_not_budgeted"
    assert db.query(TABLE, {"tenant_id": "readiness-probe"}) == []


@pytest.mark.unit
def test_a_budgeted_tier_still_draws_the_ceiling_when_the_overlay_rule_exists() -> None:
    # Positive control: the same patched registry and event shape on a real budgeted tier writes its row.
    db = InmemoryDatabaseAdapter()
    with patch(_LOAD_REGISTRY, return_value=_REGISTRY):
        result = materialize_budget_state(_event("cheap_cloud"), db)
    assert result.rows_upserted == 1
    row = db.query(TABLE, {"tenant_id": "readiness-probe"})[0]
    assert Decimal(str(row["overage_usd"])) == _OWN_KEY_COST


def _run_async_twin(tier: str) -> AsyncMock:
    """Run the async ceiling reader for ``tier`` with the overlay run's own arguments and return the database mock.

    Overlay run and control share every argument (``metered``, headroom 0.0, the same cost): only the tier name differs, so a
    control that writes while the overlay run does not can only be down to the tier rule in ``resolve_tier_cost``.
    """
    runner = DelegationProjectionRunner()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    runner._db = cast(Any, db)
    with patch(_LOAD_REGISTRY, return_value=_REGISTRY):
        asyncio.run(
            runner._materialize_budget_state_async(
                correlation_id="omn24874-own-key-run",
                cost_tier_name=tier,
                cost_measurement_source="metered",
                budget_headroom_consumed_usd=0.0,
                cost_usd=float(_OWN_KEY_COST),
                tenant_id="readiness-probe",
                timestamp=None,
            )
        )
    return db


@pytest.mark.unit
def test_tenant_overlay_run_never_draws_the_async_ceiling_twin() -> None:
    assert _run_async_twin(TENANT_OVERLAY_TIER_NAME).execute.await_args_list == []


@pytest.mark.unit
def test_a_budgeted_tier_still_draws_the_async_ceiling_twin() -> None:
    # Positive control: the overlay run's exact arguments with only the tier changed. The one statement is the budget upsert.
    db = _run_async_twin("cheap_cloud")
    assert len(db.execute.await_args_list) == 1
    call = db.execute.await_args_list[0]
    statement = call.args[0]
    assert "INSERT INTO delegation_budget_state AS s" in statement
    assert (
        "ON CONFLICT (tenant_id, cost_tier_name, budget_period) DO UPDATE" in statement
    )
    assert "cheap_cloud" in call.args


_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
# Every call site of resolve_tier_cost in src: (module path under src/omnimarket, enclosing function). Each treats None as
# "no cost model" (the measurement) or "tier not budgeted" (a ceiling reader skip); none reads a field of the result before the
# None check. A new caller turns this red until it is read the same way and listed here.
_RESOLVE_TIER_COST_CALLERS = {
    ("pricing.py", "recompute_actual_cost_and_savings"),
    ("models/priced_spend.py", "historical_unpriced_zero"),
    (
        "nodes/node_projection_delegation/handlers/handler_budget_state.py",
        "materialize_budget_state",
    ),
    (
        "nodes/node_projection_delegation/handlers/handler_delegation.py",
        "_materialize_budget_state_async",
    ),
}


def _callers_in_source(source: str, module: str) -> set[tuple[str, str]]:
    """Every (module, enclosing function) that calls ``resolve_tier_cost`` in ``source``.

    A call counts when it is written as the bare name, as an attribute
    (``pricing.resolve_tier_cost``), through an import alias, through a name bound
    to the function, or at module scope (enclosing name ``<module>``).
    """
    tree = ast.parse(source)
    names = {"resolve_tier_cost"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "resolve_tier_cost"
            )
        elif isinstance(node, ast.Assign) and (
            (isinstance(node.value, ast.Name) and node.value.id in names)
            or (isinstance(node.value, ast.Attribute) and node.value.attr in names)
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in names) or (
                isinstance(func, ast.Attribute) and func.attr in names
            ):
                found.add((module, scope))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "<module>")
    return found


def _resolve_tier_cost_callers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        found |= _callers_in_source(
            path.read_text(encoding="utf-8"), path.relative_to(_SRC_ROOT).as_posix()
        )
    return found


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "def reader():\n    return resolve_tier_cost('x')\n",
        "def reader():\n    return pricing.resolve_tier_cost('x')\n",
        "from omnimarket.pricing import resolve_tier_cost as price\ndef reader():\n    return price('x')\n",
        "import omnimarket.pricing as p\ndef reader():\n    return p.resolve_tier_cost('x')\n",
        "lookup = pricing.resolve_tier_cost\ndef reader():\n    return lookup('x')\n",
        "async def reader():\n    return pricing.resolve_tier_cost('x')\n",
    ],
)
def test_the_caller_walk_finds_every_way_of_writing_the_call(source: str) -> None:
    assert _callers_in_source(source, "m.py") == {("m.py", "reader")}


@pytest.mark.unit
def test_the_caller_walk_finds_a_module_level_call_and_ignores_other_names() -> None:
    assert _callers_in_source("TIER = pricing.resolve_tier_cost('x')\n", "m.py") == {
        ("m.py", "<module>")
    }
    # Positive control: a call to another function is not a caller.
    assert _callers_in_source("def r():\n    return other('x')\n", "m.py") == set()


@pytest.mark.unit
def test_every_resolve_tier_cost_caller_is_listed_and_reads_none_as_a_skip() -> None:
    assert _resolve_tier_cost_callers() == _RESOLVE_TIER_COST_CALLERS


@pytest.mark.unit
def test_the_unmeasured_caller_reads_none_as_no_cost_model_for_the_overlay_tier() -> (
    None
):
    # The one caller that is not a ceiling reader: with the overlay tier resolving to None it measures no_cost_model at zero
    # cost, even over a registry that declares a capped tenant_overlay tier.
    with patch(_LOAD_REGISTRY, return_value=_REGISTRY):
        measured = recompute_actual_cost_and_savings(
            tier_name=TENANT_OVERLAY_TIER_NAME,
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=None,
        )
        control = recompute_actual_cost_and_savings(
            tier_name="cheap_cloud",
            prompt_tokens=1000,
            completion_tokens=500,
            premium_counterfactual=None,
        )
    assert measured.cost_measurement_source == "no_cost_model"
    assert measured.cash_cost_usd == 0.0
    assert control.cost_measurement_source != "no_cost_model"


@pytest.mark.unit
def test_resolve_tier_cost_returns_none_for_the_overlay_tier_over_a_declaring_registry() -> (
    None
):
    with patch(_LOAD_REGISTRY, return_value=_REGISTRY):
        assert resolve_tier_cost(TENANT_OVERLAY_TIER_NAME) is None
        assert (
            resolve_tier_cost("cheap_cloud") is _CAPPED
        )  # positive control: the patched registry is the one read
