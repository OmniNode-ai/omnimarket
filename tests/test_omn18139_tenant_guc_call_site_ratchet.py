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
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_delegation.handlers import handler_delegation

_HANDLER_SOURCE_PATH = Path(inspect.getfile(handler_delegation))
_SRC_ROOT = Path(handler_delegation.__file__).resolve().parents[4]

_ADAPTER_METHODS = frozenset({"execute", "fetch", "fetchrow", "fetchval"})

#: ``CREATE POLICY <name> ON <relation> ... app.tenant_id ...`` up to the
#: statement terminator. Bounded at the ``;`` so a match cannot bleed into the
#: next statement and attribute one policy's cast to another's relation.
_POLICY_RE = re.compile(r"CREATE POLICY\s+\w+\s+ON\s+([\w.]+)([^;]*)", re.I | re.S)


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


def _relations_whose_policy_casts_the_guc() -> set[str]:
    """Relations where a slug-valued ``app.tenant_id`` ABORTS rather than narrows.

    Derived from the migrations themselves rather than from a hand-kept list,
    because the whole defect is that a relation's policy changed under a call
    site that did not change with it. A list in this file would go stale the
    same way, and silently.
    """
    casting: set[str] = set()
    for sql_path in _SRC_ROOT.rglob("*.sql"):
        text = sql_path.read_text(encoding="utf-8", errors="replace")
        for match in _POLICY_RE.finditer(text):
            relation, body = match.group(1), match.group(2)
            if "app.tenant_id" not in body:
                continue
            if "::uuid" in body:
                casting.add(relation.split(".")[-1])
    return casting


def _untenanted_adapter_calls_repo_wide() -> list[tuple[Path, int, str]]:
    """Every adapter call in the package that names no ``tenant=``, with the
    source text of the call so the relation it touches can be read off it."""
    found: list[tuple[Path, int, str]] = []
    for py_path in _SRC_ROOT.rglob("*.py"):
        try:
            source = py_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in _ADAPTER_METHODS:
                continue
            value = func.value
            is_adapter = (
                isinstance(value, ast.Attribute) and value.attr in {"db", "_db"}
            ) or (isinstance(value, ast.Name) and value.id in {"db", "adapter", "self"})
            if not is_adapter:
                continue
            if any(kw.arg == "tenant" for kw in node.keywords):
                continue
            end = node.end_lineno or node.lineno
            found.append(
                (py_path, node.lineno, "\n".join(lines[node.lineno - 1 : end]))
            )
    return found


class TestNoUntenantedCallTouchesACastingRelation:
    """OMN-18139 AC4, as a gate rather than as prose.

    The repo has adapter calls that pass no ``tenant=`` and are harmless today,
    because the relations they name carry either no policy or a TEXT-comparing
    one -- a slug-valued setting there is inert, or narrows. Converting all of
    them is not this ticket. What IS this ticket's invariant is the pairing:
    an untenanted call must never name a relation whose policy CASTS the
    setting, because that combination does not degrade, it aborts.

    Stating it as a pairing rather than as "every call must pass a tenant" is
    what makes it survivable and therefore enforceable -- and it fails closed
    from BOTH directions, which is the point: a new untenanted call on a
    casting relation is red, and so is a migration that converts a relation an
    untenanted call already touches.

    THE HONEST LIMIT, stated rather than left for someone to discover. This
    gate matches the relation NAME in the call's source text, so it is blind to
    a call whose relation is interpolated from a variable -- and both sites this
    ticket fixes were exactly that (``{exposure.table}`` and
    ``{self._table_shadow}``). This gate would NOT have caught the live defect.
    ``TestEveryAdapterCallNamesItsTenant`` above is what covers the dynamic
    case, by requiring a tenant on every call in the one module that writes
    casting relations, whatever it names. Neither gate subsumes the other, and
    the repo-wide one is deliberately the weaker of the two because it is the
    one applied to code this ticket does not own.
    """

    def test_the_casting_set_is_not_empty(self) -> None:
        """Positive control. An empty casting set makes the assertion below
        vacuously true, which would read exactly like a clean result."""
        casting = _relations_whose_policy_casts_the_guc()
        assert "delegation_events" in casting, (
            "delegation_events' tenant_isolation policy no longer parses as "
            f"casting the tenant setting (found: {sorted(casting)}) -- the "
            "gate below would pass without checking anything"
        )

    def test_the_scan_finds_untenanted_calls_to_classify(self) -> None:
        """Second positive control: the repo-wide scan still finds the calls it
        is meant to classify. Zero found would also make the gate vacuous, and
        for a different reason than an empty casting set."""
        assert len(_untenanted_adapter_calls_repo_wide()) > 0, (
            "the repo-wide scan found no untenanted adapter calls at all -- it "
            "has stopped matching the code it guards"
        )

    def test_no_untenanted_call_names_a_casting_relation(self) -> None:
        """RED before OMN-18139 on the delegation writer's aggregate re-read.

        Enumerated live 2026-09-10 at this head: 24 untenanted adapter calls
        remain in the package and NONE of them names a casting relation. The
        two that did are fixed in this change.
        """
        casting = _relations_whose_policy_casts_the_guc()
        offenders = [
            f"{path.relative_to(_SRC_ROOT)}:{lineno} names {relation!r}"
            for path, lineno, segment in _untenanted_adapter_calls_repo_wide()
            for relation in sorted(casting)
            if relation in segment
        ]
        assert offenders == [], (
            "these adapter calls pass no tenant= and name a relation whose "
            "app.tenant_id policy casts to uuid, so they run under the "
            "table-less house SLUG 'omninode' and Postgres aborts them with "
            "invalid input syntax for type uuid. Name the tenant the statement "
            f"runs as. Offenders: {offenders}"
        )
