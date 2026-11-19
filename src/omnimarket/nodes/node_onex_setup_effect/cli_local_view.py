# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local setup identity presentation shared by the public CLI facade."""

import json
from pathlib import Path

import click

from omnimarket.local_deployment.tenant_identity import (
    LOCAL_TENANT_IDENTITY_KEY,
    ModelLocalTenantIdentity,
)
from omnimarket.models.model_local_runtime_lane import ModelLocalRuntimeLane


def render_local_identity(
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
