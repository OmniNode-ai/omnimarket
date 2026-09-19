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
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain

from omnimarket.nodes.node_projection_delegation.handlers import handler_delegation
from omnimarket.projection.relation_domains import (
    UndeclaredRelationError,
    declared_relation_domain,
)

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


#: Relations named by a statement: the write target, and every read source.
_STATEMENT_RELATIONS = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$.]*)",
    re.IGNORECASE,
)


def _table_attribute_names() -> dict[str, str]:
    """``self._table_<x>`` -> the relation it holds, read from the CONTRACT.

    The handler assigns each of these from its own ``db_io.db_tables`` by
    ROLE (``self._table_generation = _by_role["generation_events"]``), so the
    mapping is resolved the same way rather than hard-coded here: a role
    renamed in the contract moves both at once.
    """
    import yaml

    source = _HANDLER_SOURCE_PATH.read_text(encoding="utf-8")
    contract = yaml.safe_load(
        (_HANDLER_SOURCE_PATH.parent.parent / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    by_role = {table["role"]: table["name"] for table in contract["db_io"]["db_tables"]}
    resolved: dict[str, str] = {}
    for match in re.finditer(
        r"self\.(_table_\w+)\s*:\s*str\s*=\s*_by_role\[\"(\w+)\"\]", source
    ):
        attribute, role = match.group(1), match.group(2)
        if role in by_role:
            resolved[attribute] = by_role[role]
    return resolved


def _statement_text(call: ast.Call) -> str | None:
    """The SQL a call issues, with ``self._table_*`` placeholders resolved.

    Every write in this handler names its relation through an f-string
    placeholder, because the relation comes from the contract rather than from
    a literal in the code. Joining only the literal parts would drop exactly
    the token this gate needs, and the statement would then name no relation
    at all -- which the caller treats as "cannot tell" and refuses. Resolving
    the placeholder is what makes the refusal mean something.
    """
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        tables = _table_attribute_names()
        parts: list[str] = []
        for value in first.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                inner = value.value
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self"
                    and inner.attr in tables
                ):
                    parts.append(tables[inner.attr])
                    continue
                # An unresolvable placeholder must not silently vanish: a name
                # the matcher cannot resolve has to read as a relation it
                # cannot classify, so the call falls back to requiring the
                # tenant rather than being exempted by an omission.
                parts.append(" __unresolved__ ")
        return "".join(parts) if parts else None
    return None


def _names_only_untenanted_relations(call: ast.Call) -> bool:
    """Whether every relation this statement names is declared non-TENANT.

    OMN-18774. The rule above is right for a TENANT relation and wrong for an
    ``omninode_internal`` or ``platform_catalog`` one: the runtime writes those
    through an operation class that refuses a ``tenant_id`` key and binds no
    ``app.tenant_id`` at all, and the relation carries no tenant column and no
    policy for a GUC to be compared against. Demanding ``tenant=`` there would
    demand the very posture the operator ruled out on 2026-09-14
    (``docs/tracking/ROLLING_WORK_LEDGER.md:654``).

    FAIL-CLOSED in three places, which is what keeps the exemption narrow: a
    call whose statement is not a literal, a statement naming no relation this
    matcher recognises, and a relation no contract declares all fall back to
    requiring the tenant. So does any statement that names even ONE tenant
    relation, whatever else it touches.
    """
    statement = _statement_text(call)
    if statement is None:
        return False
    relations = {
        match.group(1).rsplit(".", 1)[-1].lower()
        for match in _STATEMENT_RELATIONS.finditer(statement)
    }
    if not relations:
        return False
    for relation in relations:
        try:
            domain = declared_relation_domain(relation)
        except UndeclaredRelationError:
            return False
        if domain is EnumDatabaseSchemaDomain.TENANT:
            return False
    return True


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

        OMN-18774 exempts a statement all of whose relations are declared
        non-TENANT by their owning contracts -- see
        :func:`_names_only_untenanted_relations` for why the rule inverts
        there and for the three ways the exemption fails closed.
        """
        untenanted = [
            call.lineno
            for call in _adapter_calls(handler_tree)
            if not any(kw.arg == "tenant" for kw in call.keywords)
            and not _names_only_untenanted_relations(call)
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


class TestTheInternalRelationExemptionIsNarrow:
    """OMN-18774: the exemption inverts the rule for one class of relation only.

    An ``omninode_internal`` or ``platform_catalog`` relation is written
    through an operation class that refuses a ``tenant_id`` key and binds no
    ``app.tenant_id``, and after OMN-18774 carries no tenant column and no
    policy. Demanding ``tenant=`` there would demand the posture the operator
    ruled out. Everywhere else the original rule stands, and these are the
    boundaries that keep it standing.
    """

    def test_a_tenant_relation_without_a_tenant_is_still_refused(self) -> None:
        tree = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute('INSERT INTO delegation_events (a) VALUES (1)')\n"
        )
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is False

    def test_an_internal_relation_without_a_tenant_is_exempt(self) -> None:
        tree = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute('INSERT INTO generation_events (a) VALUES (1)')\n"
        )
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is True

    def test_a_statement_touching_both_is_refused(self) -> None:
        """One tenant relation anywhere in the statement ends the exemption."""
        tree = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute('SELECT 1 FROM generation_events "
            "JOIN delegation_events ON true')\n"
        )
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is False

    def test_an_undeclared_relation_is_refused_rather_than_exempted(self) -> None:
        tree = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute('INSERT INTO relation_no_contract_declares "
            "(a) VALUES (1)')\n"
        )
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is False

    def test_a_non_literal_statement_is_refused_rather_than_exempted(self) -> None:
        tree = ast.parse("async def f(self, sql):\n    await self.db.execute(sql)\n")
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is False

    def test_an_unresolvable_placeholder_is_refused_rather_than_exempted(self) -> None:
        """A name the matcher cannot resolve must not read as an absent relation."""
        tree = ast.parse(
            "async def f(self):\n"
            "    await self.db.execute(f'INSERT INTO {self._table_unknown} (a) "
            "VALUES (1)')\n"
        )
        call = _adapter_calls(tree)[0]
        assert _names_only_untenanted_relations(call) is False

    def test_the_generation_insert_is_the_call_the_exemption_covers(self) -> None:
        """Bound to the real handler, so the exemption cannot drift off it."""
        tree = ast.parse(_HANDLER_SOURCE_PATH.read_text(encoding="utf-8"))
        exempt = [
            call.lineno
            for call in _adapter_calls(tree)
            if not any(kw.arg == "tenant" for kw in call.keywords)
            and _names_only_untenanted_relations(call)
        ]
        assert len(exempt) == 1, (
            "exactly one call in this handler writes a relation its contract "
            f"declares internal; found {exempt}"
        )
