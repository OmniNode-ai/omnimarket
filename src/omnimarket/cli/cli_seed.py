# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex seed`: labelled fixture delegations for dev and demo dashboards (OMN-19970).

A shim over ``node_dev_seed_effect``; it holds no logic of its own.

* ``--bus inmemory`` (the default) seeds this install's local store through the
  same projection entry point ``onex delegate`` uses.
* ``--bus kafka --lane <lane>`` (or ``--kafka-bootstrap``) publishes the same
  terminals on the delegate-skill completed topic, with the fixture tag on each
  envelope, for that lane's projection runner to write.

Every seeded row is labelled ``data_source=fixture`` and excluded from measured
sums by default. Seeding twice writes the same rows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.events.topics import DELEGATE_SKILL_COMPLETED_TOPIC_V1
from omnimarket.nodes.node_dev_seed_effect.handlers.handler_dev_seed import (
    HandlerDevSeed,
)
from omnimarket.nodes.node_dev_seed_effect.models.model_dev_seed_request import (
    ModelDevSeedRequest,
)


async def _publish(
    messages: list[tuple[bytes, bytes]],
    *,
    lane: str | None,
    kafka_bootstrap: str | None,
    omni_home: Path | None,
) -> int:
    async with open_lab_run_bus(
        bus="kafka", lane=lane, kafka_bootstrap=kafka_bootstrap, omni_home=omni_home
    ) as bus:
        for key, value in messages:
            await bus.publish(DELEGATE_SKILL_COMPLETED_TOPIC_V1, key, value)
    return len(messages)


@click.command("seed")
@click.option(
    "--bus",
    type=click.Choice(["inmemory", "kafka"]),
    default="inmemory",
    show_default=True,
    help="inmemory seeds this install's local store; kafka publishes to a lane.",
)
@click.option(
    "--lane", default=None, help="The declared lane whose broker receives the seed."
)
@click.option("--kafka-bootstrap", default=None, help="A broker, stated directly.")
@click.option(
    "--omni-home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace root, for resolving --lane.",
)
@click.option(
    "--tenant",
    "tenant_id",
    default=None,
    help="Tenant the rows belong to. Default: this install's own identity (inmemory).",
)
def seed_command(
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    omni_home: Path | None,
    tenant_id: str | None,
) -> None:
    """Seed labelled fixture delegations through the real projection."""
    handler = HandlerDevSeed()
    if bus == "inmemory":
        if lane is not None or kafka_bootstrap is not None:
            raise click.UsageError(
                "--lane and --kafka-bootstrap name a broker; use --bus kafka"
            )
        # Imported here: the broker path must not need the local store.
        from omnimarket.local_deployment.tenant_identity import (
            ensure_install_identity_mirrored,
            resolve_local_deployment_tenant_id,
        )
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
            resolve_local_delegation_evidence_db,
        )

        db = resolve_local_delegation_evidence_db()
        tenant = resolve_local_deployment_tenant_id(tenant_id)
        ensure_install_identity_mirrored(db)
        result = handler.seed_local(db, tenant_id=tenant)
        click.echo(
            f"seeded {result.rows_projected} fixture delegations "
            f"(fixture set v{result.fixture_set_version}, data_source=fixture) "
            "into the local store"
        )
        return

    if lane is None and kafka_bootstrap is None:
        raise click.UsageError("--bus kafka needs --lane or --kafka-bootstrap")
    messages = handler.wire_messages(ModelDevSeedRequest(tenant_id=tenant_id))
    try:
        sent = asyncio.run(
            _publish(
                messages,
                lane=lane,
                kafka_bootstrap=kafka_bootstrap,
                omni_home=omni_home,
            )
        )
    except LabRunBusError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"published {sent} fixture delegations to {DELEGATE_SKILL_COMPLETED_TOPIC_V1} "
        "(data_source=fixture on each envelope)"
    )
