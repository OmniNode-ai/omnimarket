# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17446 AC5: a registry-shaped projection fed only by a time-retained
topic, with no reconcile path, must fail closed.

THE DEFECT THIS GATE EXISTS FOR. ``tenant_registry_mirror`` is the relation
both the OMN-16804 write-path resolver and migration 0034's ``USING`` JOIN
resolve tenant identity against. Its only source was
``onex.tenant.events`` at ``log.retention.hours=168``. Seven days. So four of
the five tenants that had ever written a delegation event had their
``TENANT_CREATED`` aged off the topic before the projection was ever deployed,
and no consumer restart, offset reset or redeploy could materialize them --
the events were gone. The migration's own pre-guard ("the mirror holds a row
for every distinct ``tenant_id`` in ``delegation_events``") became a condition
that could never be satisfied, and it was correctly fail-closed, so it would
have refused forever.

WHY A STATIC GATE. The failure is invisible everywhere it could be caught by a
test: the projection is correct, the resolver is correct, the migration guard
is correct. Each component does exactly what it says. The defect lives in the
JOIN between a relation's ROLE (a registry that other write paths and
migrations resolve identity against, so it must be TOTAL) and its SOURCE's
retention policy (time-retained, so it is inherently PARTIAL). Nothing in a
unit test, an integration test or a live probe of a healthy lane observes that
join -- it only becomes visible once a tenant has aged out, which is months
after the contract shipped. Detection has to be static, over the contracts and
the migration corpus, or it does not fire at all (omni_home CLAUDE.md rule 5).

WHAT THE GATE ASSERTS. For every relation a node contract declares with
``access: write``/``read_write``, where that relation is ALSO read by another
node's migration conversion clause or by a shared write-path module: the
owning contract must either subscribe to a compaction-class topic (a keyed
changelog, which retains the latest value per key indefinitely) or carry an
allowlist entry naming a real, citable reconcile path. Absence of proof is a
failure -- a topic whose cleanup policy cannot be established from the
taxonomy is treated as time-retained, because that is the fail-closed
direction.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import textwrap
from types import ModuleType

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "ci" / "check_registry_projection_reconcile.py"
_ALLOWLIST_PATH = (
    _REPO_ROOT / "scripts" / "ci" / "registry_projection_reconcile_allowlist.yaml"
)


def _load_gate() -> ModuleType:
    """Import the gate by path -- ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "omn17446_registry_projection_reconcile_gate", _GATE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_gate()


# --------------------------------------------------------------------------
# A synthetic repo tree. Built rather than mocked, because what the gate reads
# IS a filesystem layout -- a mock of it would only assert that the gate calls
# the functions the test told it to call.
# --------------------------------------------------------------------------


def _write_contract(
    root: pathlib.Path,
    node: str,
    *,
    relation: str,
    access: str = "write",
    subscribe_topics: tuple[str, ...] = ("onex.evt.some.thing.v1",),
) -> None:
    node_dir = root / "src" / "omnimarket" / "nodes" / node
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "name": node,
                "node_type": "reducer",
                "db_io": {
                    "db_tables": [
                        {
                            "name": relation,
                            "database_ref": "application",
                            "schema": "omninode_internal",
                            "migration": "0000_create.sql",
                            "access": access,
                            "role": "projection",
                        }
                    ]
                },
                "event_bus": {"subscribe_topics": list(subscribe_topics)},
            }
        ),
        encoding="utf-8",
    )


# The identity-CONVERSION shape the gate keys on: the column is rewritten
# THROUGH the joined relation, so one unresolved row refuses the whole
# migration. A migration that merely SELECTs the relation into a view is not
# this, and deliberately does not trip the gate.
_CONVERSION_SQL = """
ALTER TABLE consumer_events
    ALTER COLUMN thing_id TYPE uuid
    USING (SELECT m.thing_uuid
           FROM thing_registry_mirror m
           WHERE m.thing_slug = consumer_events.thing_id);
"""

_VIEW_SQL = """
CREATE VIEW consumer_thing_view AS
    SELECT d.*, m.thing_uuid
    FROM consumer_events d
    LEFT JOIN thing_registry_mirror m ON m.thing_slug = d.thing_id;
"""


def _write_migration(root: pathlib.Path, node: str, name: str, sql: str) -> None:
    mig_dir = root / "src" / "omnimarket" / "nodes" / node / "migrations"
    mig_dir.mkdir(parents=True, exist_ok=True)
    (mig_dir / name).write_text(textwrap.dedent(sql), encoding="utf-8")


def _write_allowlist(root: pathlib.Path, entries: list[dict[str, str]]) -> None:
    path = root / "scripts" / "ci" / "registry_projection_reconcile_allowlist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"exemptions": entries}), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A minimal tree with the two directories the gate requires."""
    (tmp_path / "src" / "omnimarket" / "nodes").mkdir(parents=True)
    (tmp_path / "src" / "omnimarket" / "projection").mkdir(parents=True)
    _write_allowlist(tmp_path, [])
    return tmp_path


# --------------------------------------------------------------------------
# The defect, reproduced
# --------------------------------------------------------------------------


class TestTheDefectFailsClosed:
    def test_registry_read_by_another_nodes_migration_on_a_retained_topic_fails(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """The OMN-17446 shape exactly: node A writes the relation off a
        time-retained topic, node B's migration resolves identity against it."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_contract(repo, "node_projection_consumer", relation="consumer_events")
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )

        violations = gate.scan(repo)

        assert [v.relation for v in violations] == ["thing_registry_mirror"]
        rendered = violations[0].render()
        assert "thing_registry_mirror" in rendered
        assert "node_projection_thing_registry" in rendered
        assert "onex.thing.events" in rendered
        # The finding must name the reader, or it is unactionable.
        assert "0034_convert.sql" in rendered

    def test_registry_read_by_a_shared_write_path_module_fails(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """The other half of registry-ness: a write path outside the owning
        node resolves identity against the relation. The real resolver composes
        its SQL by f-string (``FROM {TENANT_REGISTRY_MIRROR_TABLE}``), so a
        naive ``FROM <name>`` grep MISSES it -- the relation name reaches the
        source only as a module-level string constant."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_contract(repo, "node_projection_consumer", relation="consumer_events")
        consumer_handler = (
            repo / "src" / "omnimarket" / "nodes" / "node_projection_consumer" / "h.py"
        )
        consumer_handler.write_text(
            "from omnimarket.projection.thing_resolution import THING_REGISTRY_MIRROR_TABLE\n",
            encoding="utf-8",
        )
        (repo / "src" / "omnimarket" / "projection" / "thing_resolution.py").write_text(
            textwrap.dedent(
                '''
                """Resolve a thing identity against the registry."""
                THING_REGISTRY_MIRROR_TABLE = "thing_registry_mirror"
                _LOOKUP = f"SELECT thing_uuid FROM {THING_REGISTRY_MIRROR_TABLE}"
                '''
            ),
            encoding="utf-8",
        )

        violations = gate.scan(repo)

        assert [v.relation for v in violations] == ["thing_registry_mirror"]
        assert "thing_resolution.py" in violations[0].render()


# --------------------------------------------------------------------------
# The three ways to be clean. Each must actually clear the finding, or the
# gate is one nobody can satisfy and everyone routes around.
# --------------------------------------------------------------------------


class TestTheWaysToBeClean:
    def test_a_relation_nobody_else_reads_is_not_registry_shaped(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """An ordinary projection on a time-retained topic is FINE. A partial
        materialization of a time-series is what a projection IS. Only a
        relation another identity-resolving path depends on must be total."""
        _write_contract(
            repo,
            "node_projection_ordinary",
            relation="ordinary_events",
            subscribe_topics=("onex.evt.ordinary.v1",),
        )
        assert gate.scan(repo) == []

    def test_a_view_over_the_relation_is_not_an_identity_conversion(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """A view over a partial relation is partial, which is ordinary. Only
        an ``ALTER COLUMN ... TYPE ... USING`` conversion makes a miss fatal --
        it rewrites the column THROUGH the relation, so one unresolved row
        refuses the whole migration, permanently.

        This boundary is worth pinning: without it the gate fires on six
        savings view migrations over ``delegation_events`` and one
        capsule-effectiveness view on the real tree. A gate with seven findings
        and one real one is a gate people learn to allowlist past."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_contract(repo, "node_projection_consumer", relation="consumer_events")
        _write_migration(
            repo, "node_projection_consumer", "079_create_view.sql", _VIEW_SQL
        )
        assert gate.scan(repo) == []

    def test_a_compaction_class_topic_clears_the_finding(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """A keyed changelog retains the latest value per key indefinitely, so
        the relation can always be rebuilt from it. This is option A of the
        ticket -- available to any future registry, even though the operator
        declined it for onex.tenant.events in particular."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.snapshot.thing.registry.v1",),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        assert gate.scan(repo) == []

    def test_a_complete_allowlist_entry_clears_the_finding(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """Option B: a reconcile path that republishes the corpus from the
        authoritative source. No contract field can express one (the runtime's
        table-declaration model is ``extra="forbid"``), so the declaration is
        an annotated allowlist entry."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        _write_allowlist(
            repo,
            [
                {
                    "relation": "thing_registry_mirror",
                    "node": "node_projection_thing_registry",
                    "reconcile_path": "some_repo path/to/thing_reemit.py",
                    "ticket": "OMN-99999",
                    "reason": "republishes the corpus from the registry",
                }
            ],
        )
        assert gate.scan(repo) == []


# --------------------------------------------------------------------------
# The allowlist is the part that rots. Every field is load-bearing, and an
# entry that has outlived its finding is itself a finding.
# --------------------------------------------------------------------------


class TestTheAllowlistCannotBeRubberStamped:
    @pytest.mark.parametrize(
        "missing", ["relation", "node", "reconcile_path", "ticket", "reason"]
    )
    def test_an_entry_missing_any_required_field_is_refused(
        self, gate: ModuleType, repo: pathlib.Path, missing: str
    ) -> None:
        entry = {
            "relation": "thing_registry_mirror",
            "node": "node_projection_thing_registry",
            "reconcile_path": "some_repo path/to/thing_reemit.py",
            "ticket": "OMN-99999",
            "reason": "republishes the corpus from the registry",
        }
        del entry[missing]
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        _write_allowlist(repo, [entry])

        violations = gate.scan(repo)

        assert violations, f"a {missing}-less exemption must not clear the finding"
        assert missing in violations[0].render()

    def test_a_blank_field_is_not_a_present_field(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """An empty ``reason:`` reads as filled-in to a reviewer skimming YAML."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        _write_allowlist(
            repo,
            [
                {
                    "relation": "thing_registry_mirror",
                    "node": "node_projection_thing_registry",
                    "reconcile_path": "some_repo path/to/thing_reemit.py",
                    "ticket": "OMN-99999",
                    "reason": "   ",
                }
            ],
        )
        assert gate.scan(repo)

    def test_an_exemption_for_a_relation_with_no_finding_is_itself_a_finding(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """A stale exemption is how the next registry lands unreviewed: the
        list grows, nobody re-derives whether each line still describes a real
        constraint, and eventually one of them silently covers a relation whose
        reconcile path was deleted."""
        _write_contract(
            repo,
            "node_projection_ordinary",
            relation="ordinary_events",
            subscribe_topics=("onex.evt.ordinary.v1",),
        )
        _write_allowlist(
            repo,
            [
                {
                    "relation": "long_gone_mirror",
                    "node": "node_projection_gone",
                    "reconcile_path": "some_repo path/to/gone.py",
                    "ticket": "OMN-99999",
                    "reason": "this relation no longer exists",
                }
            ],
        )

        violations = gate.scan(repo)

        assert violations
        assert "long_gone_mirror" in violations[0].render()
        assert "stale" in violations[0].render().lower()


# --------------------------------------------------------------------------
# Fail-closed on ambiguity, and on its own inputs
# --------------------------------------------------------------------------


class TestFailsClosed:
    def test_a_topic_of_unknown_cleanup_class_is_treated_as_time_retained(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """``onex.tenant.events`` is not declared in any ``topics.yaml`` in
        this repo -- it is owned by onex-api in another repo. So the gate
        cannot READ its cleanup policy, and the honest response to "I cannot
        establish this" is to fail, not to pass."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("some.topic.with.no.taxonomy.class",),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        assert gate.scan(repo)

    def test_a_registry_with_no_subscribed_topic_at_all_still_fails(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=(),
        )
        _write_migration(
            repo,
            "node_projection_consumer",
            "0034_convert.sql",
            _CONVERSION_SQL,
        )
        assert gate.scan(repo)

    def test_an_unreadable_allowlist_is_an_invocation_error_not_a_pass(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """A gate that cannot read its own exemption list has not passed; it
        has not run."""
        (
            repo / "scripts" / "ci" / "registry_projection_reconcile_allowlist.yaml"
        ).write_text("{[ not yaml", encoding="utf-8")
        with pytest.raises(gate.GateInputError):
            gate.scan(repo)

    def test_main_refuses_outside_the_repo_root(
        self, gate: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert gate.main() == 2

    def test_the_migration_scan_ignores_the_owning_nodes_own_migrations(
        self, gate: ModuleType, repo: pathlib.Path
    ) -> None:
        """A node's own migration creating and indexing its own relation is not
        another path depending on it."""
        _write_contract(
            repo,
            "node_projection_thing_registry",
            relation="thing_registry_mirror",
            subscribe_topics=("onex.thing.events",),
        )
        _write_migration(
            repo,
            "node_projection_thing_registry",
            "0000_create.sql",
            "CREATE TABLE thing_registry_mirror (thing_slug text primary key);\n"
            + _CONVERSION_SQL,
        )
        assert gate.scan(repo) == []


# --------------------------------------------------------------------------
# Against the real tree
# --------------------------------------------------------------------------


class TestAgainstTheRealRepository:
    def test_the_real_repository_is_clean(self, gate: ModuleType) -> None:
        """If this goes red, a registry-shaped projection was added with no
        reconcile path -- fix the projection, never the allowlist."""
        assert gate.scan(_REPO_ROOT) == []

    def test_the_gate_is_probative_on_the_real_tree(self, gate: ModuleType) -> None:
        """The real clean result above is worth nothing without a positive
        control: with the tenant-registry exemption removed, the real tree must
        FAIL on the real defect. Otherwise ``scan`` could be returning ``[]``
        because it detects nothing at all."""
        violations = gate.scan(_REPO_ROOT, exemptions=())
        assert [v.relation for v in violations] == ["tenant_registry_mirror"]
        rendered = violations[0].render()
        assert "node_projection_tenant_registry" in rendered
        assert "onex.tenant.events" in rendered

    def test_the_gate_is_wired_in_both_pre_commit_and_ci(self) -> None:
        """omni_home CLAUDE.md rule 5: detection that is not a pre-merge gate
        is advisory and gets ignored. Both wirings are part of the deliverable,
        so both are pinned here -- otherwise dropping one is a silent edit that
        leaves a green build and no enforcement."""
        entry = "scripts/ci/check_registry_projection_reconcile.py"

        pre_commit = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(
            encoding="utf-8"
        )
        assert "id: registry-projection-reconcile" in pre_commit
        assert entry in pre_commit

        ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        assert "Registry-projection reconcile gate" in ci
        assert entry in ci

    def test_the_shipped_allowlist_holds_exactly_the_known_registry(self) -> None:
        """The exemption list is reviewed by reading it. Pin it, so growing it
        is a diff on this test rather than a line nobody looks at."""
        loaded = yaml.safe_load(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
        entries = loaded["exemptions"]
        assert [e["relation"] for e in entries] == ["tenant_registry_mirror"]
        entry = entries[0]
        assert entry["node"] == "node_projection_tenant_registry"
        assert entry["ticket"] == "OMN-17446"
        # The reconcile path must be a real, resolvable citation -- the
        # primitive that actually landed, not a promise.
        assert "tenant_event_reemit" in entry["reconcile_path"]
