# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict-mode auto-wiring regression harness for the typed database topology.

Background
----------
onex-dev deploy run 30737415706 (2026-08-01) failed rollout on BOTH
``omninode-runtime-worker`` and ``omninode-runtime-effects``. The worker pod's
captured auto-wiring error was, verbatim::

    Unknown schema 'public' for database_ref 'application'

raised from :meth:`omnibase_core.models.core.model_deployment_topology.ModelDeploymentTopology.schema_domain`
by way of ``omnibase_infra.runtime.auto_wiring.handler_wiring._resolve_projection_database_target``
on the ``service_kernel.bootstrap -> wire_from_manifest -> _prepare_contract_wiring
-> _prepare_handler_wiring`` boot path, with ``ONEX_WIRING_STRICT_MODE=1``.

``node_deployment_evidence_reducer`` declared ``schema: public`` for its two
projection relations. The typed topology declares exactly three schemas for the
``application`` database -- ``omninode_internal``, ``platform_catalog`` and
``tenant``. ``public`` is disallowed BY DESIGN per ADR-0027 ("Unclassified or
ambiguous relations fail closed"), so the correct repair is to classify the
relations, never to teach the validator to accept ``public``.

Why these relations are ``omninode_internal``
---------------------------------------------
1. ADR-0027 assigns the platform-internal domain to "registry, orchestration,
   **evidence**, telemetry, baseline, and operational relations".
2. The sibling ``node_evidence_dashboard_reducer`` already declares its three
   evidence relations as ``application``.``omninode_internal``.
3. 40 of the 57 relations declared across omnimarket node contracts already use
   ``omninode_internal``.

What this harness proves
------------------------
* ``test_deployment_evidence_tables_resolve_to_the_internal_domain`` drives the
  exact function that raised on the deploy (``schema_domain``) against every
  shipped topology profile. RED on the pre-fix tree with the deploy's verbatim
  error; GREEN after the contract repair.
* ``test_every_declared_db_table_resolves_or_is_a_tracked_residual`` is the
  suite-level census: EVERY ``db_io.db_tables`` declaration in EVERY omnimarket
  node contract, against EVERY shipped topology profile. The set of declarations
  that do not resolve must equal :data:`_TRACKED_UNRESOLVED_DECLARATIONS`
  exactly -- a shrink-only ratchet. A new unresolvable declaration fails CI
  instead of failing a deploy; a repaired one fails CI until it is removed from
  the ratchet. This is what converts this defect class from deploy-caught to
  CI-caught.
* ``test_internal_evidence_tables_resolve_end_to_end`` is the promoted form of
  the retired grants tripwire. The tripwire asserted the residual failure was
  EXACTLY the missing ``object_type: TABLE`` grants (OMN-15656, an
  omnibase_infra defect) — and, per its own docstring, it fired the moment the
  grants landed and the pinned rev was bumped (OMN-16077, pin ``94247acff``,
  where every shipped instance declares TABLE grants). As that docstring
  prescribed, the tripwire is deleted and replaced by the zero-failure
  assertion: the exact resolver that failed the deploy now returns a full
  binding for the evidence relations on every profile, with no exception.

References
----------
OMN-15655 (this repair), OMN-15656 (omnibase_infra topology TABLE grants),
OMN-15423 (relation classification), OMN-15418 (the validator that introduced
the strict privilege check), ADR-0027.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_core.models.core.model_deployment_topology import ModelDeploymentTopology
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _resolve_projection_database_target,
)
from omnibase_infra.topology import (
    SUPPORTED_TOPOLOGY_PROFILES,
    load_topology_profile,
)

# Repo root: tests/unit/runtime/<this file> -> parents[3] == repo root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_NODES_DIR: Final[Path] = _REPO_ROOT / "src" / "omnimarket" / "nodes"

_EVIDENCE_CONTRACT: Final[str] = "node_deployment_evidence_reducer"
_EVIDENCE_TABLES: Final[tuple[str, ...]] = (
    "deployment_evidence_projection",
    "deployment_readiness_projection",
)

# Every checked-in database-topology profile, not a hand-picked subset: a
# declaration that resolves on one lane and not another is the drift this
# census exists to catch.
_PROFILES: Final[tuple[str, ...]] = tuple(sorted(SUPPORTED_TOPOLOGY_PROFILES))

# ---------------------------------------------------------------------------
# Shrink-only ratchet.
#
# Declarations that reference a topology entity which does not exist. Each entry
# is a deliberate, ticket-bound residual, NOT an accepted state. Removing an
# entry is the only sanctioned edit; adding one requires the same operator
# classification decision ADR-0027 demands ("Unclassified or ambiguous relations
# fail closed").
#
# Shape: (node directory, relation name, declared database_ref, declared schema)
# ---------------------------------------------------------------------------
_TRACKED_UNRESOLVED_DECLARATIONS: Final[frozenset[tuple[str, str, str, str]]] = (
    frozenset(
        # RETIRED 2026-08-02 -- the nine relations that used to sit here
        # (eight `application`.`public` placeholders plus
        # node_projection_delegation's `unresolved` landmine) are gone from
        # this ratchet because the operator CLASSIFIED them, which is the
        # only sanctioned way an entry leaves: "this is all per tenant."
        # All nine now declare `schema: tenant` and resolve on every
        # profile. See OMN-15655 / OMN-15423 and the house-tenant ruling.
        #
        # CORRECTION to the retired comment, which was FALSE for two of the
        # ten entries: it claimed "None of them declares a
        # `descriptor.runtime_profiles`, so ... none is boot-fatal today."
        # `node_projection_delegation` (delegation_judge_verdict_events) and
        # `node_dispatch_outcome_bridge_effect` (dispatch_eval_results) BOTH
        # declare `runtime_profiles: [effects]`, so both were strict-mode
        # boot-fatal on the deployed effects pod, on the onex-dev green
        # path. The claim held only for the eight PUBLIC placeholders.
        #
        # RETIRED 2026-08-15 (OMN-16077 infra pin bump to 94247acff): the
        # last residual, node_dispatch_outcome_bridge_effect's
        # (`omniintelligence`.`public`.`dispatch_eval_results`), left the
        # ratchet the sanctioned way — the OMN-15668 repair landed in
        # omnibase_infra (the `omniintelligence` service-owned database is
        # now DECLARED in the typed topology, per ADR-0027), so the
        # declaration resolves on every shipped profile. The ratchet is
        # empty; it stays here so a future bad declaration still fails CI
        # against an exact (now-empty) expected set.
    )
)


def _iter_declarations() -> tuple[tuple[str, ModelDbTableDeclaration], ...]:
    """Yield (node directory, declaration) for every omnimarket db_table."""
    declarations: list[tuple[str, ModelDbTableDeclaration]] = []
    contract_paths = sorted(_NODES_DIR.glob("*/contract.yaml"))
    if not contract_paths:
        raise AssertionError(
            f"no node contracts discovered under {_NODES_DIR} — the harness "
            "repo-root derivation is stale."
        )
    for contract_path in contract_paths:
        document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        db_io = document.get("db_io") or {}
        for raw in db_io.get("db_tables") or []:
            declarations.append(
                (contract_path.parent.name, ModelDbTableDeclaration(**raw))
            )
    return tuple(declarations)


_DECLARATIONS: Final[tuple[tuple[str, ModelDbTableDeclaration], ...]] = (
    _iter_declarations()
)


def _topology(profile: str) -> ModelDeploymentTopology:
    """Load one shipped topology profile from the packaged instances."""
    # omnibase_infra is untyped for mypy in this repo (see pyproject overrides),
    # so bind the result to the declared model type explicitly.
    topology: ModelDeploymentTopology = load_topology_profile(profile)
    return topology


def _evidence_declarations() -> tuple[ModelDbTableDeclaration, ...]:
    """The two relations owned by the deployment-evidence reducer."""
    declarations = tuple(
        declaration
        for node_dir, declaration in _DECLARATIONS
        if node_dir == _EVIDENCE_CONTRACT
    )
    assert {d.name for d in declarations} == set(_EVIDENCE_TABLES), (
        f"{_EVIDENCE_CONTRACT} must declare exactly {sorted(_EVIDENCE_TABLES)}; "
        f"got {sorted(d.name for d in declarations)}"
    )
    return declarations


@pytest.mark.parametrize("profile", _PROFILES)
def test_deployment_evidence_tables_resolve_to_the_internal_domain(
    profile: str,
) -> None:
    """The relations that killed the onex-dev worker boot must resolve.

    Drives :meth:`ModelDeploymentTopology.schema_domain` -- the exact callable
    that raised ``Unknown schema 'public' for database_ref 'application'`` on
    deploy run 30737415706 -- against every shipped topology profile.
    """
    topology = _topology(profile)
    for declaration in _evidence_declarations():
        domain = topology.schema_domain(declaration.database_ref, declaration.schema)
        assert domain is EnumDatabaseSchemaDomain.OMNINODE_INTERNAL, (
            f"{profile}: {declaration.name} resolved to {domain} — deployment "
            "evidence and readiness are platform-internal relations per "
            "ADR-0027 and must resolve to the OMNINODE_INTERNAL domain."
        )


@pytest.mark.parametrize("profile", _PROFILES)
def test_every_declared_db_table_resolves_or_is_a_tracked_residual(
    profile: str,
) -> None:
    """Suite-level census: no untracked contract may reference a missing entity.

    Every ``db_io.db_tables`` declaration in every omnimarket node contract is
    resolved against the shipped topology. The unresolved set must equal the
    ticket-bound ratchet exactly, so a new bad declaration fails CI rather than
    a deploy, and a repaired declaration fails CI until it leaves the ratchet.
    """
    topology = _topology(profile)
    unresolved: set[tuple[str, str, str, str]] = set()
    for node_dir, declaration in _DECLARATIONS:
        try:
            topology.schema_domain(declaration.database_ref, declaration.schema)
        except ValueError:
            unresolved.add(
                (
                    node_dir,
                    declaration.name,
                    declaration.database_ref,
                    declaration.schema,
                )
            )

    newly_broken = sorted(unresolved - _TRACKED_UNRESOLVED_DECLARATIONS)
    assert not newly_broken, (
        f"{profile}: these db_table declarations reference a topology entity "
        f"that does not exist and are NOT tracked residuals: {newly_broken}. "
        "Classify the relation per ADR-0027 (tenant / omninode_internal / "
        "platform_catalog) — never add 'public' to the topology and never widen "
        "the validator. Strict-mode auto-wiring refuses these at runtime boot."
    )

    repaired = sorted(_TRACKED_UNRESOLVED_DECLARATIONS - unresolved)
    assert not repaired, (
        f"{profile}: these declarations now resolve and must be removed from "
        f"_TRACKED_UNRESOLVED_DECLARATIONS: {repaired}. The ratchet only shrinks."
    )


@pytest.mark.parametrize("profile", _PROFILES)
def test_internal_evidence_tables_resolve_end_to_end(
    profile: str,
) -> None:
    """The evidence relations wire fully — the promoted grants-tripwire.

    Until OMN-16077 bumped the pinned ``omnibase-infra`` rev to ``94247acff``,
    this test was ``test_internal_evidence_tables_are_blocked_only_by_absent_
    table_grants``: it asserted the ONLY residual failure was the missing
    ``object_type: TABLE`` grants (OMN-15656). The pinned topology now declares
    those grants on every shipped instance, the tripwire fired exactly as its
    docstring predicted, and this is the promotion it prescribed: the exact
    resolver that failed onex-dev deploy run 30737415706 must return a complete
    target for the evidence relations with zero failures, on every profile.
    """
    topology = _topology(profile)
    target = _resolve_projection_database_target(_evidence_declarations(), topology)
    resolved_names = {table.table.name for table in target.table_targets}
    assert resolved_names == set(_EVIDENCE_TABLES), (
        f"{profile}: expected a full binding for {sorted(_EVIDENCE_TABLES)}, "
        f"got {sorted(resolved_names)}"
    )
