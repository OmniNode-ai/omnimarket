# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17201 -- the hook-ledger writer's tenant wire prefix must name a real tenant.

THE DEFECT THIS PINS. The contract declared
``config.hook_ledger.cloud_wire_scope.tenant_slugs: [beta-gateway-canary]``.
No such tenant exists. The row in ``omninode_cloud.public.tenants`` reads
``beta-gateway-canary-79afa7263852`` (tenant_id
``79afa726-3852-464f-b7a4-d4b8b9c75ee7``), and every one of the nine
tenant-prefixed topics on ``omninode-dev-msk`` carries that long prefix --
read back live 2026-09-05 from the ``onex-api`` pod on ``onex-dev``.

WHY IT IS WORSE THAN A CRASH. While the wire topics were absent entirely, the
short spelling and the long one failed identically, so the typo was invisible
behind a louder fault. Once the topics are provisioned, a writer pointed at a
prefix no tenant owns subscribes successfully to topics nobody produces to,
reports Ready, holds LAG 0, and ingests nothing -- indistinguishable from a
working ledger. That is the silent-zero class the contract's own fail-closed
note names.

WHY THE PIN IS AGAINST ``_LEGACY_TENANT_UUID_MAP`` AND NOT A SECOND LITERAL.
Asserting the contract equals the string ``beta-gateway-canary-79afa7263852``
would just be the same hand-typed spelling written twice; a wrong value copied
into both places passes. ``_LEGACY_TENANT_UUID_MAP``
(``omnimarket/projection/tenant_isolation.py``) is this repo's existing record
of reviewed tenant identities -- the closed mapping every tenant_id-to-UUID
conversion migration inlines, cross-checked against
``omninode_cloud.public.tenants`` and documented as refusing to invent an
entry. Requiring the contract's slug to be a KEY of it makes the assertion
"this names a tenant we actually have a record of", which is the property that
was violated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
    HandlerHookLedgerProjection,
)
from omnimarket.projection.tenant_isolation import (
    UnmappedTenantIdentityError,
    resolve_tenant_uuid,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_hook_ledger"
    / "contract.yaml"
)

# Read live from omninode_cloud.public.tenants on 2026-09-05 via the onex-api
# pod on onex-dev (instance i-06169517a92b45f86), 51 rows returned.
LIVE_CANARY_TENANT_UUID = UUID("79afa726-3852-464f-b7a4-d4b8b9c75ee7")

# The exact four wire topics provisioned for that tenant (omninode_infra
# topic_constants.DEFAULT_TENANT_CANONICAL_TOPICS, OMN-17201/OMN-17051).
EXPECTED_WIRE_TOPICS = (
    "tenant-beta-gateway-canary-79afa7263852.onex.evt.omniclaude.session-started.v1",
    "tenant-beta-gateway-canary-79afa7263852.onex.evt.omniclaude.prompt-submitted.v1",
    "tenant-beta-gateway-canary-79afa7263852.onex.evt.omniclaude.tool-executed.v1",
    "tenant-beta-gateway-canary-79afa7263852.onex.evt.omniclaude.session-ended.v1",
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    with open(CONTRACT_PATH) as fh:
        loaded = yaml.safe_load(fh)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_every_declared_slug_is_a_reviewed_tenant_identity(
    contract: dict[str, Any],
) -> None:
    """The load-bearing assertion: the contract names tenants we have a record of."""
    slugs = contract["config"]["hook_ledger"]["cloud_wire_scope"]["tenant_slugs"]
    for slug in slugs:
        # Raises UnmappedTenantIdentityError on a slug with no registry entry.
        resolve_tenant_uuid(slug)


@pytest.mark.unit
def test_declared_slug_resolves_to_the_live_canary_tenant_uuid(
    contract: dict[str, Any],
) -> None:
    """Ties the slug to the specific tenant row whose topics were provisioned.

    A slug that is merely *some* known tenant would satisfy the test above; the
    writer has to point at the one tenant that actually has hook wire topics.
    """
    slugs = contract["config"]["hook_ledger"]["cloud_wire_scope"]["tenant_slugs"]
    assert [resolve_tenant_uuid(s) for s in slugs] == [LIVE_CANARY_TENANT_UUID]


@pytest.mark.unit
def test_the_short_spelling_is_not_a_tenant_identity() -> None:
    """Positive control for the assertion above.

    Without this, a registry that mapped every string would make
    ``test_every_declared_slug_is_a_reviewed_tenant_identity`` vacuous. The
    exact value the contract used to carry must be rejected.
    """
    with pytest.raises(UnmappedTenantIdentityError):
        resolve_tenant_uuid("beta-gateway-canary")


@pytest.mark.unit
def test_runner_resolves_the_wire_topics_that_exist_on_the_broker() -> None:
    """End of the chain: contract -> resolver -> the literal broker topic names.

    These four strings were read back from ``omninode-dev-msk``; if the
    resolution changes shape, this fails on the string that would have been
    subscribed to, not on an intermediate.
    """
    runner = HandlerHookLedgerProjection()
    assert set(runner.topics) == set(EXPECTED_WIRE_TOPICS)


@pytest.mark.unit
def test_the_declared_slug_is_not_derived_at_runtime_yet(
    contract: dict[str, Any],
) -> None:
    """States the residual honestly rather than implying it is closed.

    The right shape is for the runner to derive its wire prefix from the tenant
    registry -- ``tenant_registry_mirror``, materialized by
    ``node_projection_tenant_registry`` from ``onex.tenant.events``, the same
    relation ``tenant_registry_resolution`` already resolves write-time identity
    against, and the same registry the topic provisioner reads a slug from.

    That is not done here, for two reasons worth writing down rather than
    leaving for someone to rediscover. (1) The registry holds every tenant --
    51 on this plane -- and only ONE has hook wire topics, so a derivation
    without a "gateway-attached" predicate would subscribe to ~200 topics that
    do not exist and reproduce the crash-loop from the other side. (2) The slug
    is needed by ``topics`` before the runner has a database connection, so
    registry-backed resolution is a lifecycle change to ``BaseProjectionRunner``
    rather than a lookup.

    Until then the value is declared, and the tests above are what make the
    declaration checked. This test fails the moment the contract grows a
    ``tenant_ids`` key, which is the signal to delete it along with the pin.
    """
    scope = contract["config"]["hook_ledger"]["cloud_wire_scope"]
    assert set(scope) == {"tenant_slugs"}, (
        "cloud_wire_scope gained a key -- if registry-backed derivation landed, "
        "retire the literal pins in this file with it"
    )
