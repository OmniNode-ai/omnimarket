# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant``: create a tenant, read it, and mint and revoke its gateway keys, all over the bus.

A shell over the tenant handlers: it parses arguments, builds the real bus, overlay and dev store adapters and prints each
typed result as plain lines, or as JSON with ``--json``. It holds no database connection: every command is published on its
operation topic pair, read from the contract of the node that serves it, and the node's typed outcome is the answer.

Command line::

    onex tenant create --slug acme --name "Acme Ltd"
    onex tenant show --slug acme            # or --tenant-id <uuid>
    onex tenant key mint --tenant-id <uuid> --name ci [--expires-at 2027-01-01T00:00:00Z] [--store-dev-secret]
    onex tenant key revoke --tenant-id <uuid> --credential-id <uuid>

``key mint`` makes the key in this process, sends only its sha256 digest, and prints the key once on stdout after the node
answered registered. ``--store-dev-secret`` also writes it create-only to the dev secret store's ``gateway_key`` folder; it
is refused, before the store or the bus is touched, unless the command lane, the key node's overlay lane and the store's
environment all match the dev values.

Exit codes: 0 done; 2 a refusal to fix (``refused_*``, ``not_found``, ``exists``); 3 the node, the store or the bus could not
answer.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from uuid import UUID

import click
from pydantic import ValidationError

from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_create import (
    HandlerTenantCreate,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_mint import (
    HandlerTenantKeyMint,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_revoke import (
    HandlerTenantKeyRevoke,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_show import (
    HandlerTenantShow,
)
from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    ModelTenantCliResult,
    ModelTenantCreateRequest,
    ModelTenantKeyMintRequest,
    ModelTenantKeyRevokeRequest,
    ModelTenantShowRequest,
)


@dataclass(frozen=True)
class TenantHandlers:
    """The four handlers a command runs; a test builds this over fake ports."""

    create: HandlerTenantCreate
    show: HandlerTenantShow
    key_mint: HandlerTenantKeyMint
    key_revoke: HandlerTenantKeyRevoke


def build_handlers(wait_seconds: int | None = None) -> TenantHandlers:
    """The handlers over the real bus, overlay files and dev secret store."""
    from omnimarket.nodes.node_tenant_cli_effect.adapter_bus_dispatch import (
        BusTenantDispatch,
    )
    from omnimarket.nodes.node_tenant_cli_effect.adapter_dev_secret_store import (
        SecretStoreGatewayKeys,
    )
    from omnimarket.nodes.node_tenant_cli_effect.adapter_overlays import (
        FileGatewayKeyOverlays,
    )

    dispatch = BusTenantDispatch(wait_seconds=wait_seconds)
    return TenantHandlers(
        create=HandlerTenantCreate(dispatch),
        show=HandlerTenantShow(dispatch),
        key_mint=HandlerTenantKeyMint(
            dispatch, FileGatewayKeyOverlays(), SecretStoreGatewayKeys()
        ),
        key_revoke=HandlerTenantKeyRevoke(dispatch),
    )


def _handlers(ctx: click.Context) -> TenantHandlers:
    if isinstance(ctx.obj, TenantHandlers):
        return ctx.obj
    return build_handlers(ctx.meta.get("tenant_wait_seconds"))


def _emit(result: ModelTenantCliResult, *, as_json: bool) -> None:
    """Print the result and exit with its code. The key, when there is one, is on exactly one stdout line."""
    if as_json:
        document = result.model_dump(mode="json")
        if result.plaintext is not None:
            document["key"] = result.plaintext.get_secret_value()
        click.echo(json.dumps(document, sort_keys=True))
    else:
        lines = [f"outcome: {result.outcome}", f"detail: {result.detail}"]
        lines.extend(
            f"{name}: {value}" for name, value in sorted(result.fields.items())
        )
        if result.plaintext is not None:
            lines.append(f"key: {result.plaintext.get_secret_value()}")
        # A key is printed with its result on stdout; a failure goes to stderr and carries no key.
        click.echo("\n".join(lines), err=not result.ok)
    sys.exit(int(result.exit_code))


def _usage(error: ValidationError) -> click.UsageError:
    fields = sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()})
    return click.UsageError(f"invalid arguments: {fields}")


@click.group("tenant")
@click.option(
    "--wait-seconds",
    type=click.IntRange(min=1),
    default=None,
    help="How long to wait for the node's answer; the node's contract declares the default.",
)
@click.pass_context
def tenant_group(ctx: click.Context, wait_seconds: int | None) -> None:
    """Create a tenant and mint and revoke its gateway keys, over the event bus."""
    if wait_seconds is not None:
        ctx.meta["tenant_wait_seconds"] = wait_seconds


@tenant_group.command("create")
@click.option("--slug", "tenant_slug", required=True, help="The tenant slug.")
@click.option("--name", default=None, help="The tenant's display name.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
def create_command(
    ctx: click.Context, tenant_slug: str, name: str | None, as_json: bool
) -> None:
    """Create one tenant with its default plan (a repeat of the same slug answers already_exists)."""
    try:
        request = ModelTenantCreateRequest(tenant_slug=tenant_slug, name=name)
    except ValidationError as exc:
        raise _usage(exc) from None
    _emit(_handlers(ctx).create.handle(request), as_json=as_json)


@tenant_group.command("show")
@click.option("--slug", "tenant_slug", default=None, help="The tenant slug.")
@click.option("--tenant-id", type=click.UUID, default=None, help="The tenant id.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
def show_command(
    ctx: click.Context, tenant_slug: str | None, tenant_id: UUID | None, as_json: bool
) -> None:
    """Show one tenant, by slug or by id."""
    request = ModelTenantShowRequest(tenant_id=tenant_id, tenant_slug=tenant_slug)
    _emit(_handlers(ctx).show.handle(request), as_json=as_json)


key_group = click.Group("key", help="Mint and revoke a tenant's gateway keys.")
tenant_group.add_command(key_group)


@key_group.command("mint")
@click.option("--tenant-id", type=click.UUID, required=True, help="The tenant id.")
@click.option("--name", required=True, help="The key's name, unique within the tenant.")
@click.option(
    "--expires-at",
    type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"]),
    default=None,
    help="UTC expiry, e.g. 2027-01-01T00:00:00Z; the hosted overlay requires one.",
)
@click.option(
    "--store-dev-secret",
    is_flag=True,
    help="Also write the key create-only to the dev secret store; refused outside the dev lane, overlay and store.",
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
def mint_command(
    ctx: click.Context,
    tenant_id: UUID,
    name: str,
    expires_at: object,
    store_dev_secret: bool,
    as_json: bool,
) -> None:
    """Make a key here, register its digest over the bus, and print the key once."""
    from datetime import UTC, datetime

    expiry: datetime | None = None
    if isinstance(expires_at, datetime):
        expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    try:
        request = ModelTenantKeyMintRequest(
            tenant_id=tenant_id,
            name=name,
            expires_at=expiry,
            store_dev_secret=store_dev_secret,
        )
    except ValidationError as exc:
        raise _usage(exc) from None
    _emit(_handlers(ctx).key_mint.handle(request), as_json=as_json)


@key_group.command("revoke")
@click.option("--tenant-id", type=click.UUID, required=True, help="The tenant id.")
@click.option(
    "--credential-id", type=click.UUID, required=True, help="The key's credential id."
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
def revoke_command(
    ctx: click.Context, tenant_id: UUID, credential_id: UUID, as_json: bool
) -> None:
    """Revoke one key of one tenant."""
    request = ModelTenantKeyRevokeRequest(
        tenant_id=tenant_id, credential_id=credential_id
    )
    _emit(_handlers(ctx).key_revoke.handle(request), as_json=as_json)


__all__: list[str] = ["TenantHandlers", "build_handlers", "tenant_group"]
