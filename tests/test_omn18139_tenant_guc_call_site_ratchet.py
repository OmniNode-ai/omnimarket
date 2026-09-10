# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18139: every adapter call in the delegation writer names the tenant it
runs as, and the two that did not are pinned by shape.

WHY A SOURCE-LEVEL RATCHET AND NOT ONLY A BEHAVIOURAL TEST. The defect is an
ABSENT argument. ``AsyncpgAdapter._set_tenant_context`` sets ``app.tenant_id``
on every statement it issues and, when the caller passes no ``tenant=``,
synthesises one from ``resolve_read_tenant(None)`` -- the TABLE-LESS form,
which returns the house SLUG ``omninode``. Nothing about that omission is
visible at the call site: it reads like a statement with no tenant concern at
all, and it type-checks, lints and passes every mock-DB test in this repo.

It only becomes an error when the relation being touched carries a policy that
CASTS the GUC. Delegation migration 0034 made ``delegation_events.tenant_id``
a ``uuid`` and recreated ``tenant_isolation`` as
``tenant_id = current_setting('app.tenant_id', true)::uuid``, at which point the
slug stopped narrowing the read and started aborting it. So a behavioural test
proves the two sites that are wrong TODAY, and this module is what stops the
next one being discovered on staging: a new ``self.db.execute`` with no
``tenant=`` is a red test here, whether or not the relation it names casts yet.

This is Operating Rule 5 applied to a call-site property -- detection that is
not a gate does not hold.

The companion module ``tests/test_omn18139_real_postgres_tenant_guc_representation.py``
carries the real-Postgres RED/GREEN proof against the live policy shape.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_delegation.handlers import handler_delegation

_HANDLER_SOURCE_PATH = Path(inspect.getfile(handler_delegation))


def _adapter_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``self.db.execute(...)`` / ``self.db.fetch*(...)`` in the module.

    Matched structurally on the attribute chain rather than by text, so a call
    split across lines, wrapped in ``await``, or re-indented is still found --
    a regex over the source would miss exactly the reformatted call this gate
    most needs to catch.
    """
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        value = func.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "db"
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        ):
            found.append(node)
    return found


@pytest.fixture(scope="module")
def handler_tree() -> ast.Module:
    return ast.parse(_HANDLER_SOURCE_PATH.read_text(encoding="utf-8"))


class TestTheRatchetCanActuallyFail:
    """Positive control. An empty finding set reads exactly like a clean bill
    of health, so the finder is proven to find something before any zero below
    is reported as a pass."""

    def test_the_finder_locates_adapter_calls(self, handler_tree: ast.Module) -> None:
        calls = _adapter_calls(handler_tree)
        assert len(calls) >= 5, (
            "the AST matcher found almost no self.db.* calls -- it has stopped "
            "matching the code it guards, so its zero-untenanted result below "
            f"would be vacuous (found {len(calls)})"
        )

    def test_the_finder_rejects_an_untenanted_call(self) -> None:
        """The negative control: the same matcher, on a snippet that IS the
        defect, must report it. Without this the assertion below could pass
        because the predicate never fires, not because the code is clean."""
        offending = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute('SELECT 1 FROM delegation_events')\n"
        )
        calls = _adapter_calls(offending)
        assert len(calls) == 1
        assert not any(kw.arg == "tenant" for kw in calls[0].keywords)


class TestEveryAdapterCallNamesItsTenant:
    def test_no_call_reaches_the_adapter_without_a_tenant(
        self, handler_tree: ast.Module
    ) -> None:
        """RED before OMN-18139 on two lines.

        ``_publish_aggregate_snapshots``' aggregate re-read and
        ``_project_shadow_comparison``' insert both omitted ``tenant=``. The
        first one dead-lettered every delegation on onex-dev with
        ``invalid input syntax for type uuid: "omninode"``.
        """
        untenanted = [
            call.lineno
            for call in _adapter_calls(handler_tree)
            if not any(kw.arg == "tenant" for kw in call.keywords)
        ]
        assert untenanted == [], (
            "these self.db.* calls reach AsyncpgAdapter with no tenant= and so "
            "run under resolve_read_tenant(None) -- the table-less house SLUG "
            f"'omninode'. Lines: {untenanted}. Name the tenant the statement "
            "runs as; do not rely on the adapter's fallback."
        )


class TestTheAggregateRepublishRequiresItsTenant:
    """The republish must not be able to run under an unnamed tenant even by
    programming error -- the parameter is keyword-only and has no default, so
    a caller that forgets it is a TypeError rather than a silent house-slug
    bind."""

    def test_publish_aggregate_snapshots_takes_a_required_keyword_tenant(
        self,
    ) -> None:
        signature = inspect.signature(
            handler_delegation.DelegationProjectionRunner._publish_aggregate_snapshots
        )
        tenant = signature.parameters.get("tenant")
        assert tenant is not None, (
            "_publish_aggregate_snapshots no longer takes a tenant -- the "
            "aggregate re-read has gone back to the adapter's table-less "
            "fallback"
        )
        assert tenant.kind is inspect.Parameter.KEYWORD_ONLY
        assert tenant.default is inspect.Parameter.empty, (
            "a default here would reintroduce the defect: the call site could "
            "omit the tenant and the read would silently bind whatever the "
            "default names"
        )
