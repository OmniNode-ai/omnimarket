# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret`` — put your own provider key on your own machine (OMN-18695).

    onex secret set llm.openrouter.api_key      # value read from stdin
    onex secret list
    onex secret delete llm.openrouter.api_key

The resolver half of OMN-18695 makes a contract-declared ``secret_ref``
answerable only from this machine's local store. That is a refusal with nowhere
to go without a way to write to the store, so this command is part of the same
change rather than a follow-up.

WHY THE VALUE NEVER TRAVELS ON ARGV
    ``ps`` shows another user the whole command line, and an interactive shell
    writes it to history. A ``--value`` flag would therefore put the key in two
    places the store exists to keep it out of, and it would do so by default,
    which is the shape that gets used. The value is read from stdin when
    something is piped in and from a hidden prompt when a terminal is attached
    -- the same posture ``onex auth login`` already takes for its own secret.

HOW THIS COMMAND REACHES THE CLI
    It is not wired in by hand anywhere. ``omnimarket``'s ``pyproject.toml``
    advertises it in the ``onex.cli`` entry-point group and
    ``omnibase_core.cli.cli_commands`` discovers that group over the installed
    distributions, the same way ``onex cloud`` and ``onex market`` arrive
    (the 2026-08-29 operator ruling, made mechanical by OMN-16967).
"""

from __future__ import annotations

import asyncio
import sys
from getpass import getpass

import click

from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    local_credential_registered_at,
    register_local_byok_credential,
    revoke_local_byok_credential,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.local_byok_route import house_provider_slug

__all__ = ["secret_group"]

_PROMPT = "Value (input hidden): "


def _offered_provider(secret_ref: str) -> str | None:
    """The BYOK provider a declared ``llm.<provider>.<field>`` ref names, if offered.

    OMN-19205. A declared reference is what tier selection resolves, but the
    route a customer's work may run on carries a TENANT-shaped reference: the
    local BYOK substitution swaps the house-shaped rung for the catalogue's
    customer backend only when one is registered for the provider. Storing the
    declared reference alone therefore routed nowhere. ``None`` for a minted
    reference (already the customer's) and for a provider the catalogue does
    not offer (no customer backend to route to).
    """
    slug = house_provider_slug(secret_ref)
    if slug is None or resolve_byok_provider_backend(slug) is None:
        return None
    return slug


def _read_value() -> str:
    """Take the value from stdin, or from a hidden prompt on a terminal."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass(_PROMPT).strip()
    return sys.stdin.read().strip()


@click.group("secret")
def secret_group() -> None:  # stub-ok: a click group's body IS its subcommands
    """Manage the provider credentials this machine resolves references to.

    Values are held in this machine's local store with owner-only file
    permissions. They are NOT encrypted at rest: anyone who can read the file
    as its owner, or who is handed a copy of it, can read the key.
    """


@secret_group.command("set")
@click.argument("secret_ref")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Replace a value already stored under this reference.",
)
def set_secret(secret_ref: str, force: bool) -> None:
    """Store the value for SECRET_REF, read from stdin.

    The value is never taken from an argument. Pipe it in
    (``printf %s "$KEY" | onex secret set <ref>``) or let the command prompt
    for it with the input hidden.
    """
    store = LocalByokCredentialStore()
    if not force and asyncio.run(store.get_secret(secret_ref)) is not None:
        raise click.ClickException(
            f"{secret_ref} already has a stored value. Pass --force to "
            "replace it. Refusing by default so a re-run of a setup script "
            "cannot silently swap a working credential for a stale one."
        )

    value = _read_value()
    if not value:
        raise click.ClickException(
            f"no value was supplied for {secret_ref}. Pipe the value in, or "
            "run this on a terminal to be prompted for it. The value is never "
            "read from a command-line argument."
        )

    asyncio.run(store.set_secret(secret_ref, value))
    click.echo(f"Stored {secret_ref} in {store.db_path} (owner-only).")
    provider = _offered_provider(secret_ref)
    if provider is not None:
        # The same key, under the tenant-shaped reference the customer route
        # carries. Replaces any earlier one for this provider (one key each).
        route_ref = register_local_byok_credential(
            provider, value, db_path=store.db_path
        )
        click.echo(f"Registered it as your {provider} route key: {route_ref}.")


@secret_group.command("list")
def list_secrets() -> None:
    """List the references this machine holds. Never prints a value."""
    store = LocalByokCredentialStore()
    refs = asyncio.run(store.list_keys())
    if not refs:
        click.echo(
            "This machine holds no provider credentials. Register one with: "
            "onex secret set <reference>"
        )
        return

    click.echo(f"{len(refs)} reference(s) in {store.db_path}:")
    for ref in refs:
        updated = local_credential_registered_at(ref) or "unknown"
        click.echo(f"  {ref}  (updated {updated})")


@secret_group.command("delete")
@click.argument("secret_ref")
def delete_secret(secret_ref: str) -> None:
    """Remove the stored value for SECRET_REF."""
    store = LocalByokCredentialStore()
    if not asyncio.run(store.delete_secret(secret_ref)):
        raise click.ClickException(
            f"this machine holds no value for {secret_ref}; nothing was "
            "removed. Run 'onex secret list' to see what is stored."
        )
    click.echo(f"Removed {secret_ref}.")
    provider = _offered_provider(secret_ref)
    if provider is not None and revoke_local_byok_credential(
        provider, db_path=store.db_path
    ):
        click.echo(f"Withdrew your {provider} route key with it.")
