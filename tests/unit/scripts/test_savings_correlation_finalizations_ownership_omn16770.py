# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16770 step 1: declare `savings_correlation_finalizations` ownership here.

`omnibase_infra`'s ``node_savings_estimation_compute`` decides which sessions
are ready to finalize with an anti-join. Until OMN-16770's durable close that
anti-join read ``savings_estimates`` — a TENANT relation under ``FORCE ROW
LEVEL SECURITY`` that the node neither owns nor writes — and the OMN-16770
seam (``_assert_idempotency_read_is_scoped``) refuses the batch outright on
any connection where that read cannot answer truthfully. On every compose
lane the correlation pool connects as ``omninode_runtime`` (NOSUPERUSER /
NOBYPASSRLS / non-owner, OMN-16843), so the refusal is permanent: the batch
has never run.

The close is to stop reading a TENANT relation for INTERNAL idempotency and
track finalization in the node's own ``omninode_internal`` domain. That needs
a NEW relation, and the OMN-15361 SQL ownership gate
(``omnibase_infra/scripts/ci/check_application_database_sql.py``) requires
exactly one ownership declaration for every application relation a CHANGED
deployable SQL file targets — resolved from service manifests only, never
from a node's own ``contract.yaml``, and read from ``omnimarket@dev``.

So this declaration has to reach ``omnimarket@dev`` BEFORE omnibase_infra can
land the migration, exactly like the OMN-16180 ``work_events`` and OMN-16293
``savings_injection_signals`` precedents beside it. This test pins that
declaration so the sequencing cannot be undone by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "scripts" / "application-relation-ownership.yaml"

RELATION = "savings_correlation_finalizations"
MIGRATION = (
    "docker/migrations/forward/nodes/node_savings_estimation_compute/"
    "0002_create_savings_correlation_finalizations.sql"
)


def _declarations() -> list[dict[str, Any]]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    tables = manifest["db_io"]["db_tables"]
    assert isinstance(tables, list)
    return [table for table in tables if isinstance(table, dict)]


def _declaration(name: str) -> dict[str, Any]:
    matches = [table for table in _declarations() if table.get("name") == name]
    if not matches:
        pytest.fail(
            f"{MANIFEST.relative_to(REPO_ROOT)} declares no ownership entry for "
            f"`{name}`. The OMN-15361 ownership gate resolves declarations from "
            f"this manifest only, so omnibase_infra cannot land "
            f"{MIGRATION} until this entry is on omnimarket@dev (OMN-16770)."
        )
    assert len(matches) == 1, f"`{name}` is declared {len(matches)} times, expected 1"
    return matches[0]


@pytest.mark.unit
def test_savings_correlation_finalizations_is_declared_internal_domain() -> None:
    """The marker relation is INTERNAL-domain, which is the whole point.

    Declaring it ``schema: tenant`` would reproduce the defect the close
    exists to remove: the anti-join would again read a relation under a
    GUC-predicated row-level-security policy that this node carries no scope
    to bind, and the OMN-16770 seam would refuse the batch forever.
    """
    declaration = _declaration(RELATION)
    assert declaration["database_ref"] == "application"
    assert declaration["schema"] == "omninode_internal"
    assert declaration["access"] == "write"


@pytest.mark.unit
def test_savings_correlation_finalizations_names_the_omnibase_infra_migration() -> None:
    """The declaration points at the CREATE that will land in omnibase_infra.

    Catalog-only entry, same shape as the ``savings_injection_signals`` /
    ``savings_validator_catch_signals`` entries beside it: the node lives
    entirely in omnibase_infra and has no omnimarket-side ``contract.yaml``
    ``db_io`` declaration to register.
    """
    assert _declaration(RELATION)["migration"] == MIGRATION


@pytest.mark.unit
def test_sibling_savings_signal_declarations_are_unchanged() -> None:
    """The two OMN-16293 entries this one is modelled on are not disturbed."""
    for sibling in ("savings_injection_signals", "savings_validator_catch_signals"):
        declaration = _declaration(sibling)
        assert declaration["schema"] == "omninode_internal"
        assert declaration["access"] == "write"


@pytest.mark.unit
def test_savings_estimates_stays_tenant_domain() -> None:
    """The close does NOT reclassify `savings_estimates`.

    Reclassifying the TENANT relation to make the old anti-join legal was the
    other available direction and it is the wrong one — the relation really is
    per-tenant. The close removes the cross-domain READ instead. If this
    assertion ever fails, the durable close was replaced by a reclassification.
    """
    assert _declaration("savings_estimates")["schema"] == "tenant"
