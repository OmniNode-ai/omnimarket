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
from omnimarket.routing.byok_plan_detection import detect_byok_plan
from omnimarket.routing.byok_provider_backends import (
    byok_provider_plans,
    resolve_byok_provider_backend,
)
from omnimarket.routing.local_byok_route import house_provider_slug

__all__ = ["secret_group"]

#: The leading characters a key for a known provider starts with. Checked only
#: on the terminal prompt, where a mistyped or mis-pasted value is the failure;
#: a piped value is a script's choice and is stored as given. A provider absent
#: here has no documented fixed prefix, so nothing is checked for it.
_KNOWN_KEY_PREFIXES: dict[str, str] = {"openrouter": "sk-or-v1-"}


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


def _resolve_plan(
    provider: str | None, value: str, plan_option: str | None
) -> str | None:
    """The plan this key registers under, decided BEFORE anything is stored.

    OMN-20157. ``None`` for a provider that is not offered or has one plan:
    nothing to choose. Otherwise the named plan, checked against the catalogue,
    or the plan detection finds. When neither yields one the command stops and
    stores nothing, because a key filed under the wrong product routes to an
    endpoint that refuses it and reads as a billing failure.

    The key is sent only to the provider's own declared endpoints, by
    :func:`detect_byok_plan`, and is never echoed.
    """
    if provider is None:
        if plan_option is not None:
            raise click.ClickException(
                "--plan applies to a provider the catalogue offers; this "
                "reference names none."
            )
        return None
    plans = byok_provider_plans(provider)
    if plan_option is not None:
        named = plan_option.strip().lower()
        if named not in plans:
            raise click.ClickException(
                f"{provider} has no plan {plan_option!r}. Choose one of: "
                f"{', '.join(plans)}. Nothing was stored."
            )
        return named
    if len(plans) <= 1:
        return None
    click.echo(
        f"{provider} has more than one plan ({', '.join(plans)}). Trying your key "
        "against each with a one-token request to find which is yours."
    )
    detection = asyncio.run(detect_byok_plan(provider, value))
    if detection.plan is not None:
        click.echo(f"Detected plan: {detection.plan}.")
        return detection.plan
    if detection.outcome == "rejected":
        raise click.ClickException(
            f"every {provider} plan refused that key. Check that you copied the "
            "whole key from the provider's key page. Nothing was stored."
        )
    if detection.outcome == "ambiguous":
        raise click.ClickException(
            f"more than one {provider} plan accepts that key, and they are metered "
            "differently, so this command will not choose for you. Run it again "
            f"with --plan {' or --plan '.join(plans)}. Nothing was stored."
        )
    raise click.ClickException(
        f"could not tell which {provider} plan that key belongs to (a plan could "
        "not be reached, or answered with a throttle). Run the command again "
        f"with --plan {' or --plan '.join(plans)}. Nothing was stored."
    )


def _stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _prompt_value(secret_ref: str) -> str:
    """Ask for the value on a terminal: hidden, confirmed, and shape-checked.

    Nothing typed is ever echoed or included in a message, including in the
    refusals below.
    """
    value = getpass(f"Paste the value for {secret_ref} (input hidden): ").strip()
    if not value:
        raise click.ClickException(
            f"no value was entered for {secret_ref}; nothing was stored."
        )
    slug = house_provider_slug(secret_ref)
    prefix = _KNOWN_KEY_PREFIXES.get(slug) if slug is not None else None
    if prefix is not None and not value.startswith(prefix):
        raise click.ClickException(
            f"that does not look like a {slug} key: a {slug} key starts with "
            f"{prefix!r}. Nothing was stored. Copy the whole key from the "
            "provider's key page and run the command again."
        )
    if getpass("Paste it again to confirm (input hidden): ").strip() != value:
        raise click.ClickException(
            "the two entries did not match; nothing was stored. Run the command again."
        )
    return value


def _read_value(secret_ref: str) -> str:
    """Take the value from stdin when piped, or from a prompt on a terminal."""
    if _stdin_is_tty():
        return _prompt_value(secret_ref)
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
@click.option(
    "--plan",
    "plan_option",
    default=None,
    help=(
        "The provider product this key belongs to, for a provider that has more "
        "than one (glm: coding_plan or general_api). Omit it and the key is "
        "tried against each product to find out."
    ),
)
def set_secret(secret_ref: str, force: bool, plan_option: str | None) -> None:
    """Store the value for SECRET_REF, read from stdin.

    The value is never taken from an argument. Pipe it in
    (``printf %s "$KEY" | onex secret set <ref>``) or let the command prompt
    for it with the input hidden.

    For a provider with more than one plan, the plan is detected by sending the
    key to each plan's endpoint as a one-token request, or named with --plan
    (which sends nothing).
    """
    store = LocalByokCredentialStore()
    if not force and asyncio.run(store.get_secret(secret_ref)) is not None:
        raise click.ClickException(
            f"{secret_ref} already has a stored value. Pass --force to "
            "replace it. Refusing by default so a re-run of a setup script "
            "cannot silently swap a working credential for a stale one."
        )

    value = _read_value(secret_ref)
    if not value:
        raise click.ClickException(
            f"no value was supplied for {secret_ref}. Pipe the value in, or "
            "run this on a terminal to be prompted for it. The value is never "
            "read from a command-line argument."
        )

    provider = _offered_provider(secret_ref)
    plan = _resolve_plan(provider, value, plan_option)
    asyncio.run(store.set_secret(secret_ref, value))
    click.echo(f"Stored {secret_ref} in {store.db_path} (owner-only).")
    if provider is not None:
        # The same key, under the tenant-shaped reference the customer route
        # carries. Replaces any earlier one for this provider (one key each).
        route_ref = register_local_byok_credential(
            provider, value, plan=plan, db_path=store.db_path
        )
        suffix = f" (plan: {plan})" if plan is not None else ""
        click.echo(f"Registered it as your {provider} route key{suffix}: {route_ref}.")


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
