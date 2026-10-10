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
import fcntl
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from typing import Any, cast

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
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    PROVIDERS,
    HandlerModelSetup,
    key_looks_like,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelTestResult,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.credential_publisher import (
    CredentialPlanUndeterminedError,
    CredentialStoreError,
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
    ModelInferenceCredentialCreateRequest,
    ProtocolCredentialEventBus,
    register_inference_credential,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing.byok_model_discovery import (
    describe_discovery_refusal,
    discover_byok_model_sync,
    model_not_chosen_message,
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

__all__ = ["delete_secret_value", "models_group", "secret_group", "store_secret_value"]

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


def _ask_model(provider: str) -> str:
    """The model the customer names at a terminal; a script is refused (OMN-20844).

    Called only for a provider whose catalogue row says the customer chooses
    the model and when no ``--model`` was given. Nothing is picked for them.
    """
    if not _stdin_is_tty():
        raise click.ClickException(
            f"{model_not_chosen_message(provider)} Nothing was stored."
        )
    label = _MODEL_LABELS.get(provider, provider)
    answer = str(
        click.prompt(
            f"Which {label} model should your key run? Paste its id from "
            f"{label}'s model page",
            type=str,
        )
    ).strip()
    if not answer:
        raise click.ClickException(
            f"{model_not_chosen_message(provider)} Nothing was stored."
        )
    return answer


def _check_chosen_model(
    provider: str, plan: str | None, value: str, chosen: str
) -> str:
    """Check the customer's model against their key's own list (OMN-20844).

    A model the list does not name, or a key the provider refuses, is refused
    and nothing is stored. A list that cannot be read stores the model as
    chosen, unchecked, and says so.
    """
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None:
        return chosen
    click.echo(f"Checking that {provider} lists {chosen} for your key.")
    discovery = discover_byok_model_sync(backend, value, chosen=chosen)
    refusal = describe_discovery_refusal(discovery)
    if refusal is not None:
        raise click.ClickException(f"{refusal} Nothing was stored.")
    if discovery.model is None:
        click.echo(
            f"Model: {chosen} (your choice; {provider}'s model list could not be "
            "read just now, so it could not be checked)."
        )
        return chosen
    click.echo(f"Model: {chosen} (your choice, listed for your key).")
    return chosen


def _resolve_model(
    provider: str,
    plan: str | None,
    value: str,
    known: str | None,
    chosen: str | None = None,
) -> str | None:
    """The model this key's route will run, decided BEFORE anything is stored.

    OMN-20157. The catalogue pins no model: it declares a preference, and the
    provider's own model list for THIS key says what it may use (a provider
    retires ids for new accounts while old ones keep them). ``known`` is a model
    plan detection already resolved. A key the provider refuses, or whose list
    names no preferred model, is refused here with the provider's words and
    nothing is stored. A list that cannot be read stores no model, and the
    first delegation resolves it instead.

    OMN-20844. ``chosen`` is the model the customer named with ``--model``; it
    is checked against the key's list and stored as named. For a provider whose
    catalogue row says the customer chooses the model, no ``--model`` means the
    customer is asked at a terminal and a script is refused: the preference is
    never used to pick one for them.
    """
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if chosen is None and backend is not None and backend.customer_chooses_model:
        chosen = _ask_model(provider)
    if chosen is not None:
        return _check_chosen_model(provider, plan, value, chosen)
    if known is not None:
        click.echo(f"Model: {known} (the best match your key's model list offers).")
        return known
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
@click.option(
    "--model",
    "model_option",
    default=None,
    help="The model this key runs, checked against the provider's list for it.",
)
def register_tenant_key(
    provider: str,
    tenant: str,
    name: str | None,
    plan: str | None,
    model_option: str | None,
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
            model=model_option,
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
                "model": response.model,
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
_REGISTERED_IDS = ("tenant_id", "provider", "name", "api_key_ref")
_REVOKED_IDS = ("tenant_id", "api_key_ref")


def _pending_events_path(db_path: Path) -> Path:
    return db_path.parent / PENDING_CREDENTIAL_EVENTS_NAME


@contextmanager
def _pending_lock(db_path: Path) -> Iterator[None]:
    """Hold this store's pending-events lock, across processes, for one step.

    Two ``onex secret`` commands can overlap. Without the lock one command's
    drain could remove a batch the other saved while the drain ran, and two
    failed folds saving at once could each rewrite the file without the other's
    batch. Every read-modify-write of the pending file happens under it.
    """
    path = db_path.parent / f"{PENDING_CREDENTIAL_EVENTS_NAME}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
        # A decode error is a ValueError, so a file that is not UTF-8 is refused too.
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise refused from None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            raise refused from None
        if not _whole_event(record):
            raise refused
        lines.append(record)
    return lines


def _whole_event(record: object) -> bool:
    """True for a ``{"kind", "payload"}`` record whose payload is a whole event.

    Every record is checked before any is applied, so a damaged line refuses the
    file instead of being folded as a no-op (the folds return quietly on a
    missing id) and then dropped with the rest.
    """
    if not isinstance(record, dict) or set(record) != {"kind", "payload"}:
        return False
    model: type[ModelCredentialRegisteredEvent | ModelCredentialRevokedEvent]
    required: tuple[str, ...]
    if record["kind"] == _REGISTERED:
        model, required = ModelCredentialRegisteredEvent, _REGISTERED_IDS
    elif record["kind"] == _REVOKED:
        model, required = ModelCredentialRevokedEvent, _REVOKED_IDS
    else:
        return False
    try:
        event = model.model_validate(record["payload"])
    except ValidationError:
        return False
    return all(str(getattr(event, field)).strip() for field in required)


def _drain_pending_credential_events(db_path: Path) -> None:
    """Apply Credentials-page updates an earlier command could not, then forget them.

    Runs first in every ``onex secret`` command, so a fold lost to a transient
    failure is recovered by whichever command comes next, in the order the
    events were produced, before any new store change.
    """
    path = _pending_events_path(db_path)
    if not path.exists():
        return
    with _pending_lock(db_path):
        if not path.exists():
            return
        lines = _read_pending(path)
        try:
            _apply_events(lines, db_path)
        except sqlite3.Error as error:
            raise click.ClickException(
                f"the Credentials page still has updates waiting in {path} and "
                f"they could not be applied ({type(error).__name__}: {error}); "
                "they are kept. Run 'onex secret list' again once the store is "
                "readable."
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
            with _pending_lock(db_path):
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
@click.option(
    "--model",
    "model_option",
    default=None,
    help=(
        "The model this provider key runs, checked against the provider's list "
        "for the key. Required for a provider where you choose the model "
        "(openrouter): without it a terminal is asked and a script is refused."
    ),
)
def set_secret(
    secret_ref: str, force: bool, plan_option: str | None, model_option: str | None
) -> None:
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

    store_secret_value(
        secret_ref,
        value,
        force=force,
        plan_option=plan_option,
        model_option=model_option,
    )


def store_secret_value(
    secret_ref: str,
    value: str,
    *,
    force: bool,
    plan_option: str | None = None,
    model_option: str | None = None,
) -> None:
    """Store ``value`` under ``secret_ref`` and register a provider key's route.

    The body of ``onex secret set`` once the value is in hand, shared with
    ``onex models add`` so both store a key the same way: plan and model
    resolved first, the key stored and registered by the local secret store
    effect, and its credential events folded into the local store.
    """
    store = LocalByokCredentialStore()
    provider = _offered_provider(secret_ref)
    plan, detected_model = _resolve_plan(provider, value, plan_option)
    if provider is None and model_option is not None:
        raise click.ClickException(
            "--model applies to a provider key the catalogue offers; this "
            "reference names none. Nothing was stored."
        )
    model = (
        _resolve_model(provider, plan, value, detected_model, model_option)
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
    delete_secret_value(secret_ref)


def delete_secret_value(secret_ref: str) -> None:
    """Remove ``secret_ref``'s value and withdraw its route key, if any.

    The body of ``onex secret delete``, shared with ``onex models remove``.
    """
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


# ---------------------------------------------------------------------------
# ``onex models`` (OMN-20817): set up the models delegation can use. A shim like
# ``onex secret`` above: the key is stored by store_secret_value, and status and
# the pinned test delegation are node_model_setup_effect's.
# ---------------------------------------------------------------------------

_MODEL_LABELS: dict[str, str] = {
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "ollama": "Ollama",
    "anthropic": "Anthropic (Claude)",
}
_KEY_PAGES: dict[str, str] = {
    "gemini": "aistudio.google.com/apikey",
    "openrouter": "openrouter.ai/keys",
    "openai": "platform.openai.com/api-keys",
}
_OLLAMA_HOW = (
    "Ollama is installed by onboarding, which downloads a model sized to this "
    "Mac: run onboarding again with --provider ollama."
)


def _model_handler() -> HandlerModelSetup:
    return HandlerModelSetup()


def _show_model_test(result: ModelModelTestResult, as_json: bool) -> None:
    if as_json:
        click.echo(result.model_dump_json())
        return
    label = f"{_MODEL_LABELS[result.provider]:<11}"
    if result.status == "passed":
        click.echo(f"{label} ✓ answered ({result.model or result.backend_id})")
    elif result.status == "failed":
        click.echo(f"{label} ✗ {result.reason}")
        if result.provider != "ollama":
            click.echo(f"  Fix it, then run: onex models test {result.provider}")
    elif result.provider == "ollama":
        click.echo(f"{label} not set up. {_OLLAMA_HOW}")
    else:
        click.echo(
            f"{label} not set up. Add it with: onex models add {result.provider}"
        )


def _read_model_key(provider: str) -> str:
    """The key from stdin when piped, else one hidden prompt. Never echoed."""
    label = _MODEL_LABELS[provider]
    if _stdin_is_tty():
        value = getpass(
            f"Paste your {label} API key ({_KEY_PAGES[provider]}; input is hidden): "
        ).strip()
    else:
        value = sys.stdin.read().strip()
    if not value:
        raise click.ClickException(f"no {label} key was entered; nothing was stored.")
    looks = key_looks_like(value)
    if looks is not None and looks != provider:
        raise click.ClickException(
            f"that looks like a key for {_MODEL_LABELS[looks]}, not {label}. "
            "Nothing was stored."
        )
    return value


_model_choice = click.Choice(PROVIDERS, case_sensitive=False)
_model_json = click.option(
    "--json", "as_json", is_flag=True, help="One JSON object per line."
)


models_group = click.Group(
    "models",
    help=(
        "Set up the models delegation can use: Gemini, OpenRouter, OpenAI, "
        "Ollama.\n\nSet up any combination; delegation chooses among them for "
        "each task."
    ),
)


@models_group.command("add")
@click.argument("provider", type=_model_choice)
@click.option(
    "--model",
    "model_option",
    default=None,
    help=(
        "The model your key runs, checked against the provider's list for it. "
        "For OpenRouter you choose it: without --model a terminal is asked and "
        "a script is refused."
    ),
)
@_model_json
def add_model(provider: str, model_option: str | None, as_json: bool) -> None:
    """Store PROVIDER's key, then test it with one delegation pinned to it.

    The key is read from stdin when piped, or asked for at a hidden prompt.
    A key already stored for PROVIDER is replaced. The test passes only when it
    answers on the model stored for the key.
    """
    name = provider.lower()
    handler = _model_handler()
    if name == "ollama":
        if not handler.is_set_up("ollama"):
            raise click.ClickException(_OLLAMA_HOW)
        if model_option is not None:
            raise click.ClickException(
                f"--model does not apply to Ollama. {_OLLAMA_HOW}"
            )
    else:
        store_secret_value(
            f"llm.{name}.api_key",
            _read_model_key(name),
            force=True,
            model_option=model_option,
        )
    request = ModelModelSetupRequest(operation="test", provider=name)
    (result,) = handler.handle(request).tests
    _show_model_test(result, as_json)
    if result.status != "passed":
        sys.exit(1)


@models_group.command("test")
@click.argument("provider", required=False, type=_model_choice)
@_model_json
def test_models(provider: str | None, as_json: bool) -> None:
    """Test PROVIDER, or every set-up provider, with one pinned delegation each."""
    request = ModelModelSetupRequest(
        operation="test",
        provider=provider.lower() if provider else None,
    )
    tests = _model_handler().handle(request).tests
    if not tests:
        raise click.ClickException(
            "no model is set up. Add one with: onex models add <provider>"
        )
    for result in tests:
        _show_model_test(result, as_json)
    if any(result.status != "passed" for result in tests):
        sys.exit(1)


@models_group.command("list")
@_model_json
def list_models(as_json: bool) -> None:
    """Each provider: set up or not, and its last test result."""
    status = _model_handler().handle(ModelModelSetupRequest(operation="status"))
    for row in status.providers:
        if as_json:
            click.echo(row.model_dump_json())
            continue
        label = f"{_MODEL_LABELS[row.provider]:<11}"
        last = row.last_test
        if not row.set_up:
            click.echo(f"{label} not set up")
        elif last is None:
            click.echo(f"{label} set up, not tested yet")
        elif last.status == "passed":
            click.echo(
                f"{label} set up, last test passed {last.tested_at} ({last.model})"
            )
        else:
            click.echo(
                f"{label} set up, last test FAILED {last.tested_at}: {last.reason}"
            )


@models_group.command("remove")
@click.argument("provider", type=_model_choice)
def remove_model(provider: str) -> None:
    """Delete PROVIDER's stored key so delegation no longer uses it."""
    name = provider.lower()
    handler = _model_handler()
    if name == "ollama":
        raise click.ClickException(
            f"Ollama's routes are in {handler.overrides_path()}; delete that file "
            "to stop using Ollama, and quit the Ollama app."
        )
    if not handler.is_set_up(cast(ModelProvider, name)):
        raise click.ClickException(f"no {_MODEL_LABELS[name]} key is stored.")
    delete_secret_value(f"llm.{name}.api_key")
    handler.handle(ModelModelSetupRequest(operation="forget", provider=name))
