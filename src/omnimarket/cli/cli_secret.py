# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret`` — put your own provider key on your own machine (OMN-18695).

    onex secret set llm.openrouter.api_key      # value read from stdin
    onex secret register-tenant-key openrouter --tenant dev   # stdin -> ref/event
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
import json
import os
import sqlite3
import sys
from getpass import getpass
from pathlib import Path
from typing import Any

import click
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    local_credential_registered_at,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
    LocalSecretStoreRefusedError,
    offered_provider,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_result import (
    ModelLocalSecretResult,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.credential_publisher import (
    CredentialPlanUndeterminedError,
    CredentialStoreError,
    ModelCredentialRegisteredEvent,
    ModelInferenceCredentialCreateRequest,
    ProtocolCredentialEventBus,
    register_inference_credential,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing.byok_model_discovery import (
    describe_discovery_refusal,
    discover_byok_model_sync,
)
from omnimarket.routing.byok_plan_detection import detect_byok_plan
from omnimarket.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
    byok_provider_plans,
    byok_routable_plans,
    require_byok_plan_permitted,
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
    return offered_provider(secret_ref)


def _refuse_plan_not_permitted(provider: str, plan: str) -> None:
    """Stop with the plan's typed refusal when ``plan`` is detection-only.

    OMN-20157. The catalogue declares z.ai's Coding Plan ``customer_routable:
    false`` because the provider's terms bar its quota from third-party systems
    (knowledge-base-internal ``reference/zai-glm-coding-plan-terms.md``), so a
    key for it is never stored or routed. The message carries the typed code and
    says what to register instead.
    """
    try:
        require_byok_plan_permitted(provider, plan)
    except ByokPlanNotPermittedError as refusal:
        raise click.ClickException(f"{refusal} Nothing was stored.") from refusal


def _resolve_plan(
    provider: str | None, value: str, plan_option: str | None
) -> tuple[str | None, str | None]:
    """The plan this key registers under, decided BEFORE anything is stored.

    Returns ``(plan, model)``. ``model`` is the model detection already resolved
    for the detected plan from the key's own model list, or ``None`` when
    detection did not run.

    OMN-20157. ``None`` for a provider that is not offered or has one plan:
    nothing to choose. Otherwise the named plan, checked against the catalogue,
    or the plan detection finds. When neither yields one the command stops and
    stores nothing, because a key filed under the wrong product routes to an
    endpoint that refuses it and reads as a billing failure.

    A plan the catalogue declares detection-only (z.ai's Coding Plan) is refused
    with its typed code whether it is named with ``--plan`` (no network is used)
    or found by detection: a customer's Coding Plan key is never stored or routed.

    The key is sent only to the provider's own declared endpoints, by
    :func:`detect_byok_plan`, and is never echoed.
    """
    if provider is None:
        if plan_option is not None:
            raise click.ClickException(
                "--plan applies to a provider the catalogue offers; this "
                "reference names none."
            )
        return None, None
    declared = byok_provider_plans(provider)
    routable = byok_routable_plans(provider)
    if plan_option is not None:
        named = plan_option.strip().lower()
        if named not in declared:
            raise click.ClickException(
                f"{provider} has no plan {plan_option!r}. Choose one of: "
                f"{', '.join(routable)}. Nothing was stored."
            )
        _refuse_plan_not_permitted(provider, named)
        return named, None
    if len(declared) <= 1:
        return None, None
    click.echo(
        f"{provider} has more than one plan ({', '.join(declared)}). Trying your "
        "key with a one-token request to find which is yours."
    )
    detection = asyncio.run(detect_byok_plan(provider, value))
    if detection.refused_plan is not None:
        _refuse_plan_not_permitted(provider, detection.refused_plan)
    if detection.plan is not None:
        _refuse_plan_not_permitted(provider, detection.plan)
        click.echo(f"Detected plan: {detection.plan}.")
        return detection.plan, detection.model
    if detection.outcome == "rejected":
        raise click.ClickException(
            f"every {provider} plan refused that key. Check that you copied the "
            "whole key from the provider's key page. Nothing was stored."
        )
    if detection.outcome == "ambiguous":
        raise click.ClickException(
            f"more than one {provider} plan accepts that key, and they are metered "
            "differently, so this command will not choose for you. Run it again "
            f"with --plan {' or --plan '.join(routable)}. Nothing was stored."
        )
    raise click.ClickException(
        f"could not tell which {provider} plan that key belongs to (a plan could "
        "not be reached, or answered with a throttle). Run the command again "
        f"with --plan {' or --plan '.join(routable)}. Nothing was stored."
    )


def _resolve_model(
    provider: str, plan: str | None, value: str, known: str | None
) -> str | None:
    """The model this key's route will run, decided BEFORE anything is stored.

    OMN-20157. The catalogue pins no model: it declares a preference, and the
    provider's own model list for THIS key says what it may use (a provider
    retires ids for new accounts while old ones keep them). ``known`` is a model
    plan detection already resolved. A key the provider refuses, or whose list
    names no preferred model, is refused here with the provider's words and
    nothing is stored. A list that cannot be read stores no model, and the
    first delegation resolves it instead.
    """
    if known is not None:
        click.echo(f"Model: {known} (the best match your key's model list offers).")
        return known
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None:
        return None
    click.echo(f"Asking {provider} which models your key can use.")
    discovery = discover_byok_model_sync(backend, value)
    refusal = describe_discovery_refusal(discovery)
    if refusal is not None:
        raise click.ClickException(f"{refusal} Nothing was stored.")
    if discovery.model is None:
        click.echo(
            f"Could not read {provider}'s model list just now; the model will be "
            "chosen from it at your first delegation."
        )
        return None
    click.echo(
        f"Model: {discovery.model} (the best match among the "
        f"{discovery.listed_count} models your key can use)."
    )
    return discovery.model


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


def _tenant_key_store() -> LocalByokCredentialStore:
    """Use the SQLite credential store under this runtime's HOME."""
    return LocalByokCredentialStore()


def _tenant_key_event_bus() -> ProtocolCredentialEventBus | None:
    """Let the publisher construct and own this runtime's Settings-backed bus.

    Tests may return an injected bus; production passes None so bootstrap and
    producer lifecycle follow the same path as hosted credential intake.
    """
    return None


@secret_group.command("register-tenant-key")
@click.argument("provider")
@click.option("--tenant", required=True, help="Tenant that owns this provider key.")
@click.option("--name", default=None, help="Credential label; defaults to PROVIDER.")
@click.option("--plan", default=None, help="Provider product; omit to detect the plan.")
def register_tenant_key(
    provider: str, tenant: str, name: str | None, plan: str | None
) -> None:
    """Register a tenant's provider key from stdin and publish its reference."""
    if not tenant.strip():
        raise click.ClickException("--tenant must not be empty; nothing was stored.")
    value = _read_value(f"llm.{provider}.api_key")
    if not value:
        raise click.ClickException("no value was read from stdin; nothing was stored.")
    try:
        request = ModelInferenceCredentialCreateRequest(
            name=name if name is not None else provider,
            provider=provider,
            key_value=SecretStr(value),
            plan=plan,
        )
        response = asyncio.run(
            register_inference_credential(
                request,
                tenant_id=tenant,
                secret_store=_tenant_key_store(),
                event_bus=_tenant_key_event_bus(),
            )
        )
    except ValidationError as exc:
        # Pydantic input dumps can contain caller data. Surface only the
        # catalogue/field refusal messages, with no request representation.
        message = "; ".join(
            error["msg"] for error in exc.errors(include_input=False, include_url=False)
        )
        raise click.ClickException(message) from exc
    except (
        CredentialStoreError,
        CredentialPlanUndeterminedError,
        ByokPlanNotPermittedError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(
            {
                "api_key_ref": response.api_key_ref,
                "provider": response.provider,
                "plan": response.plan,
                "tenant": tenant,
            }
        )
    )


#: Credential events a failed fold could not apply, kept beside the store until
#: the next ``onex secret`` command applies them. The store change happens before
#: the fold, so re-running the same command cannot redo a lost fold: a retried
#: delete finds no value and emits no revoke. Each line is one event, and an
#: event carries a fingerprint prefix and a set time, never a value.
PENDING_CREDENTIAL_EVENTS_NAME = "credential-events.pending.jsonl"
_REGISTERED = "registered"
_REVOKED = "revoked"


def _pending_events_path(db_path: Path) -> Path:
    return db_path.parent / PENDING_CREDENTIAL_EVENTS_NAME


def _apply_events(lines: list[dict[str, Any]], db_path: Path) -> None:
    """Apply ``{"kind", "payload"}`` records in order. Both folds are idempotent."""
    db = SqliteDatabaseAdapter(db_path)
    for line in lines:
        if line["kind"] == _REGISTERED:
            apply_credential_registered(line["payload"], db)
        else:
            apply_credential_revoked(line["payload"], db)


def _write_pending(path: Path, lines: list[dict[str, Any]]) -> None:
    """Replace the pending file with ``lines``, owner-only, in one rename."""
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _read_pending(path: Path) -> list[dict[str, Any]]:
    """The pending records, or a refusal naming the file. Never drops a line."""
    refused = click.ClickException(
        f"{path} holds Credentials-page updates that could not be read; nothing "
        "was applied or dropped. Inspect the file, and remove it only if the "
        "page already shows what it should."
    )
    lines: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise refused from None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            raise refused from None
        if (
            not isinstance(record, dict)
            or record.get("kind") not in (_REGISTERED, _REVOKED)
            or not isinstance(record.get("payload"), dict)
        ):
            raise refused
        lines.append(record)
    return lines


def _drain_pending_credential_events(db_path: Path) -> None:
    """Apply Credentials-page updates an earlier command could not, then forget them.

    Runs first in every ``onex secret`` command, so a fold lost to a transient
    failure is recovered by whichever command comes next, in the order the
    events were produced, before any new store change.
    """
    path = _pending_events_path(db_path)
    if not path.exists():
        return
    lines = _read_pending(path)
    try:
        _apply_events(lines, db_path)
    except sqlite3.Error as error:
        raise click.ClickException(
            f"the Credentials page still has updates waiting in {path} and they "
            f"could not be applied ({type(error).__name__}: {error}); they are "
            "kept. Run 'onex secret list' again once the store is readable."
        ) from None
    path.unlink()


def _fold_credential_events(result: ModelLocalSecretResult, db_path: Path) -> None:
    """Fold the effect's credential events into the local projection store.

    Mode 1 has no broker: the local runtime folds a node's events in-process
    into the SQLite store the local dashboard serves, as the delegation and
    metering folds do. That store is the same file as the secret store (the
    2026-09-18 ruling: one local database), and the rows carry the fingerprint
    prefix and set time, never the value.

    The store change has already happened, so a fold that fails keeps its events
    in the pending file for the next ``onex secret`` command to apply.
    """
    if not result.events:
        return
    lines: list[dict[str, Any]] = [
        {
            "kind": _REGISTERED
            if isinstance(event, ModelCredentialRegisteredEvent)
            else _REVOKED,
            "payload": event.model_dump(mode="json"),
        }
        for event in result.events
    ]
    done = "removed" if result.operation == "delete" else "stored"
    try:
        _apply_events(lines, db_path)
    except sqlite3.Error as error:
        path = _pending_events_path(db_path)
        cause = f"{type(error).__name__}: {error}"
        try:
            earlier = _read_pending(path) if path.exists() else []
            _write_pending(path, earlier + lines)
        except (OSError, click.ClickException):
            raise click.ClickException(
                f"{result.secret_ref} is {done}, but the Credentials page was not "
                f"updated ({cause}), and the update could not be saved to {path} "
                "either. The page may show this key wrongly until it is set or "
                "deleted again."
            ) from None
        raise click.ClickException(
            f"{result.secret_ref} is {done}, but the Credentials page was not "
            f"updated ({cause}). The update is saved in {path}; run "
            "'onex secret list' to apply it."
        ) from None


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
        "than one (glm: general_api). Omit it and the key is tried to find out. "
        "A key for a plan the provider's terms bar from third-party systems (glm "
        "Coding Plan) is refused, never stored."
    ),
)
def set_secret(secret_ref: str, force: bool, plan_option: str | None) -> None:
    """Store the value for SECRET_REF, read from stdin.

    The value is never taken from an argument. Pipe it in
    (``printf %s "$KEY" | onex secret set <ref>``) or let the command prompt
    for it with the input hidden.

    For a provider with more than one plan, the plan is detected by sending the
    key to the provider's endpoints as a one-token request, or named with --plan
    (which sends nothing).
    """
    store = LocalByokCredentialStore()
    _drain_pending_credential_events(store.db_path)
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
    plan, detected_model = _resolve_plan(provider, value, plan_option)
    model = (
        _resolve_model(provider, plan, value, detected_model)
        if provider is not None
        else None
    )
    # OMN-19985: the local secret store effect stores the key and, for a provider
    # key, registers it under a freshly minted route ref (replacing any earlier
    # one for this provider, one key each) and returns the credential events.
    try:
        result = HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(
                operation="set",
                secret_ref=secret_ref,
                value=SecretStr(value),
                force=force,
                plan=plan,
                model=model,
            )
        )
    except LocalSecretStoreRefusedError as refusal:
        raise click.ClickException(str(refusal)) from None
    click.echo(f"Stored {secret_ref} in {store.db_path} (owner-only).")
    route_ref = result.route_ref
    if provider is not None and route_ref is not None:
        details = [
            f"plan: {plan}" if plan is not None else None,
            f"model: {model}" if model is not None else None,
        ]
        shown = [detail for detail in details if detail is not None]
        suffix = f" ({', '.join(shown)})" if shown else ""
        click.echo(f"Registered it as your {provider} route key{suffix}: {route_ref}.")
    _fold_credential_events(result, store.db_path)


@secret_group.command("list")
def list_secrets() -> None:
    """List the references this machine holds. Never prints a value."""
    store = LocalByokCredentialStore()
    _drain_pending_credential_events(store.db_path)
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
    _drain_pending_credential_events(store.db_path)
    try:
        result = HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(operation="delete", secret_ref=secret_ref)
        )
    except LocalSecretStoreRefusedError as refusal:
        raise click.ClickException(str(refusal)) from None
    click.echo(f"Removed {secret_ref}.")
    if result.route_withdrawn:
        click.echo(f"Withdrew your {result.provider} route key with it.")
    _fold_credential_events(result, store.db_path)
