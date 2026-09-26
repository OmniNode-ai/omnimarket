# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``onex local`` -- the local deployment's own install-time surface (OMN-18699).

Two subcommands:

``onex local init``
    Mint this install's tenant identity, once, and declare its runtime lane
    (OMN-19750): select the local-home overlay source and write the shipped
    ``runtime.lane`` example document there. Idempotent: a second run reports
    what it already has and changes nothing.

``onex local identity``
    Print it. Read-only, and refuses rather than inventing one when the install
    has never been initialised.

**Why a new group rather than an existing entrypoint.** The natural candidates
all fail on one of two grounds. ``onex init``, ``onex config init``,
``onex bootstrap apply`` and ``onex install`` live in ``omnibase_core``, which
cannot import ``omnimarket`` -- the dependency runs the other way, so the
distribution that owns the local delegation store is not reachable from any of
them. ``onex market`` is in the right distribution but is scoped, in its own
group docstring, to discovering and installing ONEX market packages; an identity
mint there would be a second meaning for one word. ``onex delegate`` is a
command, not a group, and lives in ``omnibase_infra``. So this is a new group in
the distribution that owns the store, and it is deliberately the only one.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import click

from omnimarket.local_deployment.runtime_lane import (
    LocalRuntimeLaneError,
    ModelLocalRuntimeLane,
    declare_local_runtime_lane,
)
from omnimarket.local_deployment.tenant_identity import (
    LOCAL_TENANT_IDENTITY_KEY,
    LocalTenantIdentityError,
    ModelLocalTenantIdentity,
    mint_local_tenant_identity,
    read_local_tenant_identity,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path

__all__ = ["identity_command", "init_command", "local_group"]


def _render(
    identity: ModelLocalTenantIdentity,
    *,
    store: Path,
    as_json: bool,
    lane: ModelLocalRuntimeLane | None = None,
) -> None:
    if as_json:
        payload: dict[str, object] = {
            "key": LOCAL_TENANT_IDENTITY_KEY,
            "tenant_id": str(identity.tenant_uuid),
            "tenant_slug": identity.tenant_slug,
            "recorded_at": identity.recorded_at.isoformat(),
            "newly_minted": identity.newly_minted,
            "store": str(store),
        }
        if lane is not None:
            payload["runtime_lane"] = {
                "lane_id": lane.lane_id,
                "environment": lane.environment,
                "path": str(lane.path),
                "sha256": lane.sha256,
                "newly_written": lane.newly_written,
            }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    verb = "minted" if identity.newly_minted else "already present"
    click.echo(f"local deployment tenant identity ({verb})")
    click.echo(f"  tenant_id:   {identity.tenant_uuid}")
    click.echo(f"  tenant_slug: {identity.tenant_slug}")
    click.echo(f"  key:         {LOCAL_TENANT_IDENTITY_KEY}")
    click.echo(f"  store:       {store}")
    if lane is not None:
        verb = "written" if lane.newly_written else "already present"
        click.echo(f"local runtime lane ({verb})")
        click.echo(f"  lane_id:     {lane.lane_id}")
        click.echo(f"  environment: {lane.environment}")
        click.echo(f"  document:    {lane.path}")
        click.echo(f"  sha256:      {lane.sha256}")


@click.group("local")
def local_group() -> None:  # stub-ok: click group, subcommands added below
    """Manage this local ONEX deployment."""


@local_group.command("init")
@click.option(
    "--tenant-id",
    "tenant_id",
    type=str,
    default=None,
    help=(
        "Adopt an identity this install's existing records already carry, "
        "instead of minting a fresh one. Refused if it disagrees with an "
        "identity already recorded here."
    ),
)
@click.option(
    "--tenant-slug",
    "tenant_slug",
    type=str,
    default=None,
    help="The registry-mirror key for this identity. Derived from it if omitted.",
)
@click.option(
    "--store",
    "store",
    type=click.Path(path_type=Path),
    default=None,
    help="Local delegation store. Defaults to this install's own.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine JSON.")
def init_command(
    tenant_id: str | None,
    tenant_slug: str | None,
    store: Path | None,
    as_json: bool,
) -> None:
    """Mint this install's tenant identity and declare its runtime lane. Safe to re-run."""
    resolved_store = store or default_evidence_db_path()
    parsed: UUID | None = None
    if tenant_id is not None:
        try:
            parsed = UUID(tenant_id)
        except ValueError as exc:
            # Refused rather than slugified: an identity that is not a UUID
            # cannot be confirmed against the registry mirror, so accepting one
            # here would only move the failure to the first evidence write.
            raise click.ClickException(
                f"--tenant-id must be a UUID; got {tenant_id!r}"
            ) from exc
    try:
        identity = mint_local_tenant_identity(
            db_path=resolved_store, tenant_uuid=parsed, tenant_slug=tenant_slug
        )
    except LocalTenantIdentityError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        lane = declare_local_runtime_lane()
    except LocalRuntimeLaneError as exc:
        raise click.ClickException(str(exc)) from exc
    _render(identity, store=resolved_store, as_json=as_json, lane=lane)


@local_group.command("identity")
@click.option(
    "--store",
    "store",
    type=click.Path(path_type=Path),
    default=None,
    help="Local delegation store. Defaults to this install's own.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine JSON.")
def identity_command(store: Path | None, as_json: bool) -> None:
    """Print this install's tenant identity, or refuse if it has none."""
    resolved_store = store or default_evidence_db_path()
    try:
        identity = read_local_tenant_identity(db_path=resolved_store)
    except LocalTenantIdentityError as exc:
        raise click.ClickException(str(exc)) from exc
    if identity is None:
        raise click.ClickException(
            "this install has never minted a tenant identity. Run "
            "`onex local init` once. No identity will be invented for it."
        )
    _render(identity, store=resolved_store, as_json=as_json)
