# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Add, list and remove a model key: the one path ``onex models`` and ``onex secret`` run (OMN-20816).

A developer manages the providers delegation can use in the terminal (``onex
models add|list|remove``, and ``onex secret set|delete`` for any reference).
Both run this module, and the local dashboard's Credentials page lists the
providers through its ``list_providers``, read-only: no key goes to the page
or comes from it. Removing the last usable provider is refused here, so every
caller of ``remove_key`` gets the same refusal.

It composes two nodes the way the CLI always has: the key is stored and
registered by ``node_local_secret_store_effect``, and status and the pinned
test delegation are ``node_model_setup_effect``'s. Nothing here prompts or
prints: a caller passes ``echo`` for progress lines and ``ask_model`` for a
terminal's model question, and a refusal is a :class:`ModelKeyRefusedError`
whose text says what to do instead. No key value is ever put in a message.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_plan,
    resolve_local_byok_credential_ref,
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
    ModelSetupRefusedError,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
    ModelProvider,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelStatus,
    ModelModelTestResult,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
    apply_credential_revoked,
)
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
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
    byok_automatic_plans,
    byok_provider_plans,
    byok_routable_plans,
    require_byok_plan_permitted,
    resolve_byok_provider_backend,
)

#: A progress line for whoever is watching: the terminal prints it, a script drops it.
Echo = Callable[[str], None]
#: A terminal's question for the model a key runs; a caller with no terminal passes none.
AskModel = Callable[[str], str]


#: The ``echo`` for a caller with no one watching, such as a script: a deque that holds no items, so each line
#: is discarded as it is appended. Lines are never logged (they name secret references).
_quiet: Echo = deque[str](maxlen=0).append


class ModelKeyRefusedError(Exception):
    """A key change refused before it happened. The text says what to do instead."""


class LastUsableModelRefusedError(ModelKeyRefusedError):
    """Removing this provider would leave delegation no usable model (OMN-20816)."""


MODEL_LABELS: dict[str, str] = {
    "gemini": "Gemini",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "glm": "GLM",
    "ollama": "Ollama",
    "local": "Local URL",
}
OLLAMA_HOW = (
    "Ollama is installed by onboarding, which downloads a model sized to this "
    "Mac: run onboarding again with --provider ollama."
)
LOCAL_HOW = (
    "Declare a model server on an OpenAI-compatible URL (LM Studio, llama.cpp, "
    "vLLM, MLX) with: onex models add local --url <base url> --model <name>"
)

#: The providers the dashboard's Credentials page lists (OMN-20816 decision (b)):
#: the ticket's four. Anthropic and a local URL are managed in the terminal.
DASHBOARD_PROVIDERS: tuple[ModelProvider, ...] = (
    "gemini",
    "openrouter",
    "openai",
    "ollama",
)
#: The providers whose key ``remove_key`` deletes.
KEYED_PROVIDERS: tuple[ModelProvider, ...] = (
    "gemini",
    "openrouter",
    "openai",
    "anthropic",
    "glm",
)


# ---------------------------------------------------------------------------
# The plan and the model a key registers under, decided before anything is stored.
# ---------------------------------------------------------------------------


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
    false`` because the provider's terms bar its quota from third-party systems,
    so a key for it is never stored or routed. The message carries the typed code and
    says what to register instead.
    """
    try:
        require_byok_plan_permitted(provider, plan)
    except ByokPlanNotPermittedError as refusal:
        raise ModelKeyRefusedError(f"{refusal} Nothing was stored.") from refusal


def resolve_plan(
    provider: str | None, value: str, plan_option: str | None, echo: Echo = _quiet
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
            raise ModelKeyRefusedError(
                "--plan applies to a provider the catalogue offers; this "
                "reference names none."
            )
        return None, None
    declared = byok_provider_plans(provider)
    routable = byok_routable_plans(provider)
    if plan_option is not None:
        named = plan_option.strip().lower()
        if named not in declared:
            raise ModelKeyRefusedError(
                f"{provider} has no plan {plan_option!r}. Choose one of: "
                f"{', '.join(routable)}. Nothing was stored."
            )
        _refuse_plan_not_permitted(provider, named)
        return named, None
    if len(byok_automatic_plans(provider)) <= 1:
        return None, None
    echo(
        f"{provider} has more than one plan ({', '.join(declared)}). Trying your "
        "key with a one-token request to find which is yours."
    )
    detection = asyncio.run(detect_byok_plan(provider, value))
    if detection.refused_plan is not None:
        _refuse_plan_not_permitted(provider, detection.refused_plan)
    if detection.plan is not None:
        _refuse_plan_not_permitted(provider, detection.plan)
        echo(f"Detected plan: {detection.plan}.")
        return detection.plan, detection.model
    if detection.outcome == "rejected":
        raise ModelKeyRefusedError(
            f"every {provider} plan refused that key. Check that you copied the "
            "whole key from the provider's key page. Nothing was stored."
        )
    if detection.outcome == "ambiguous":
        raise ModelKeyRefusedError(
            f"more than one {provider} plan accepts that key, and they are metered "
            "differently, so this command will not choose for you. Run it again "
            f"with --plan {' or --plan '.join(routable)}. Nothing was stored."
        )
    raise ModelKeyRefusedError(
        f"could not tell which {provider} plan that key belongs to (a plan could "
        "not be reached, or answered with a throttle). Run the command again "
        f"with --plan {' or --plan '.join(routable)}. Nothing was stored."
    )


def check_chosen_model(
    provider: str, plan: str | None, value: str, chosen: str, echo: Echo = _quiet
) -> str:
    """Check the customer's model against their key's own list (OMN-20844).

    A model the list does not name, or a key the provider refuses, is refused
    and nothing is stored. A list that cannot be read stores the model as
    chosen, unchecked, and says so.
    """
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None:
        return chosen
    echo(f"Checking that {provider} lists {chosen} for your key.")
    discovery = discover_byok_model_sync(backend, value, chosen=chosen)
    refusal = describe_discovery_refusal(discovery)
    if refusal is not None:
        raise ModelKeyRefusedError(f"{refusal} Nothing was stored.")
    if discovery.model is None:
        echo(
            f"Model: {chosen} (your choice; {provider}'s model list could not be "
            "read just now, so it could not be checked)."
        )
        return chosen
    echo(f"Model: {chosen} (your choice, listed for your key).")
    return chosen


def resolve_model(
    provider: str,
    plan: str | None,
    value: str,
    known: str | None,
    chosen: str | None = None,
    *,
    ask_model: AskModel | None = None,
    echo: Echo = _quiet,
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
    customer is asked (``ask_model``, a terminal) and a caller that cannot ask is
    refused: the preference is never used to pick one for them.
    """
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if chosen is None and backend is not None and backend.customer_chooses_model:
        if ask_model is None:
            raise ModelKeyRefusedError(
                f"{model_not_chosen_message(provider)} Nothing was stored."
            )
        chosen = ask_model(provider)
    if chosen is not None:
        return check_chosen_model(provider, plan, value, chosen, echo)
    if known is not None:
        echo(f"Model: {known} (the best match your key's model list offers).")
        return known
    if backend is None:
        return None
    echo(f"Asking {provider} which models your key can use.")
    discovery = discover_byok_model_sync(backend, value)
    refusal = describe_discovery_refusal(discovery)
    if refusal is not None:
        raise ModelKeyRefusedError(f"{refusal} Nothing was stored.")
    if discovery.model is None:
        echo(
            f"Could not read {provider}'s model list just now; the model will be "
            "chosen from it at your first delegation."
        )
        return None
    echo(
        f"Model: {discovery.model} (the best match among the "
        f"{discovery.listed_count} models your key can use)."
    )
    return discovery.model


def model_chosen_by(
    provider: str | None,
    plan: str | None,
    model: str | None,
    model_option: str | None,
) -> Literal["customer", "catalogue"] | None:
    """OMN-21078: record the chooser, including a prompted customer choice."""
    if model is None:
        return None
    backend = (
        resolve_byok_provider_backend(provider, plan=plan)
        if provider is not None
        else None
    )
    if model_option is not None or (
        backend is not None and backend.customer_chooses_model
    ):
        return "customer"
    return "catalogue"


# ---------------------------------------------------------------------------
# The Credentials-page fold, and the pending file a failed fold leaves behind.
# ---------------------------------------------------------------------------

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


@contextmanager
def _removal_lock(db_path: Path) -> Iterator[None]:
    """Hold the provider-removal lock across processes for one removal.

    The last-usable check and the deletion are one step under it: two
    overlapping ``onex models remove`` commands could otherwise each see the
    other provider usable and both delete, leaving none (OMN-20816).
    """
    path = db_path.parent / "model-removal.lock"
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
    refused = ModelKeyRefusedError(
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


def drain_pending_credential_events(db_path: Path) -> None:
    """Apply Credentials-page updates an earlier command could not, then forget them.

    Runs first in every ``onex secret`` command and every key change, so a fold
    lost to a transient failure is recovered by whichever comes next, in the
    order the events were produced, before any new store change.
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
            raise ModelKeyRefusedError(
                f"the Credentials page still has updates waiting in {path} and "
                f"they could not be applied ({type(error).__name__}: {error}); "
                "they are kept. Run 'onex secret list' again once the store is "
                "readable."
            ) from None
        path.unlink()


def fold_credential_events(result: ModelLocalSecretResult, db_path: Path) -> None:
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
        except (OSError, ModelKeyRefusedError):
            raise ModelKeyRefusedError(
                f"{result.secret_ref} is {done}, but the Credentials page was not "
                f"updated ({cause}), and the update could not be saved to {path} "
                "either. The page may show this key wrongly until it is set or "
                "deleted again."
            ) from None
        raise ModelKeyRefusedError(
            f"{result.secret_ref} is {done}, but the Credentials page was not "
            f"updated ({cause}). The update is saved in {path}; run "
            "'onex secret list' to apply it."
        ) from None


# ---------------------------------------------------------------------------
# Store and delete one reference: the body of ``onex secret set|delete``.
# ---------------------------------------------------------------------------


def store_key(
    secret_ref: str,
    value: str,
    *,
    force: bool,
    plan_option: str | None = None,
    model_option: str | None = None,
    ask_model: AskModel | None = None,
    echo: Echo = _quiet,
) -> ModelLocalSecretResult:
    """Store ``value`` under ``secret_ref`` and register a provider key's route.

    The body of ``onex secret set`` once the value is in hand, shared with
    ``onex models add`` and the dashboard so all three store a key the same way:
    plan and model resolved first, the key stored and registered by the local
    secret store effect, and its credential events folded into the local store.
    """
    store = LocalByokCredentialStore()
    provider = _offered_provider(secret_ref)
    plan, detected_model = resolve_plan(provider, value, plan_option, echo)
    if provider is None and model_option is not None:
        raise ModelKeyRefusedError(
            "--model applies to a provider key the catalogue offers; this "
            "reference names none. Nothing was stored."
        )
    model = (
        resolve_model(
            provider,
            plan,
            value,
            detected_model,
            model_option,
            ask_model=ask_model,
            echo=echo,
        )
        if provider is not None
        else None
    )
    chooser = model_chosen_by(provider, plan, model, model_option)
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
                model_chosen_by=chooser,
            )
        )
    except LocalSecretStoreRefusedError as refusal:
        raise ModelKeyRefusedError(str(refusal)) from None
    echo(f"Stored {secret_ref} in {store.db_path} (owner-only).")
    if provider in PROVIDERS:
        # OMN-21259: a new key starts untested; the old key's failed test
        # must not keep it out of routing.
        model_handler().handle(
            ModelModelSetupRequest(operation="forget", provider=provider)
        )
    route_ref = result.route_ref
    if provider is not None and route_ref is not None:
        details = [
            f"plan: {plan}" if plan is not None else None,
            f"model: {model}" if model is not None else None,
        ]
        shown = [detail for detail in details if detail is not None]
        suffix = f" ({', '.join(shown)})" if shown else ""
        echo(f"Registered it as your {provider} route key{suffix}: {route_ref}.")
    fold_credential_events(result, store.db_path)
    return result


def _keyed_provider_of(secret_ref: str) -> ModelProvider | None:
    """The keyed provider whose key ``secret_ref`` names, or None.

    Either name counts: a declared ``llm.<provider>.<field>`` reference, as the
    store's own delete classifies it (it withdraws that provider's route), or
    the provider's live minted route reference, matched through the store's
    resolver rather than by parsing the reference.
    """
    declared = _offered_provider(secret_ref)
    if declared is not None:
        return next((p for p in KEYED_PROVIDERS if p == declared), None)
    db_path = LocalByokCredentialStore().db_path
    for provider in KEYED_PROVIDERS:
        if secret_ref == resolve_local_byok_credential_ref(provider, db_path=db_path):
            return provider
    return None


def delete_key(secret_ref: str, echo: Echo = _quiet) -> ModelLocalSecretResult:
    """Remove ``secret_ref``'s value and withdraw its route key, if any.

    The body of ``onex secret delete``, shared with ``onex models remove`` and
    the dashboard. A provider's key takes the same removal lock and last-usable
    guard as ``onex models remove``, so the lower-level command cannot remove
    the last usable model either (OMN-20816).
    """
    provider = _keyed_provider_of(secret_ref)
    if provider is None:
        return _delete_key(secret_ref, echo)
    with _removal_lock(LocalByokCredentialStore().db_path):
        refuse_last_usable(provider)
        return _delete_key(secret_ref, echo)


def _delete_key(secret_ref: str, echo: Echo) -> ModelLocalSecretResult:
    """:func:`delete_key` without the guard, for a caller already holding the lock."""
    store = LocalByokCredentialStore()
    drain_pending_credential_events(store.db_path)
    try:
        result = HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(operation="delete", secret_ref=secret_ref)
        )
    except LocalSecretStoreRefusedError as refusal:
        raise ModelKeyRefusedError(str(refusal)) from None
    echo(f"Removed {secret_ref}.")
    if result.route_withdrawn:
        echo(f"Withdrew your {result.provider} route key with it.")
    fold_credential_events(result, store.db_path)
    return result


# ---------------------------------------------------------------------------
# A provider's key, as ``onex models`` manages it and the Credentials page lists it.
# ---------------------------------------------------------------------------


def _write_model_list(provider: str, models: tuple[str, ...]) -> tuple[str, ...]:
    """Store a key's whole ordered model list through the local secret store node."""
    try:
        written = HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(
                operation="set_models",
                secret_ref=f"llm.{provider}.api_key",
                models=models,
                model_chosen_by="customer",
            )
        )
    except LocalSecretStoreRefusedError as refusal:
        raise ModelSetupRefusedError(str(refusal)) from None
    return written.models


def model_handler() -> HandlerModelSetup:
    """The setup effect with the model-list write it is handed (it imports no other node)."""
    return HandlerModelSetup(write_model_list=_write_model_list)


def add_key(
    provider: str,
    key: str,
    models: tuple[str, ...],
    *,
    ask_model: AskModel | None = None,
    echo: Echo = _quiet,
) -> ModelModelTestResult:
    """Store ``provider``'s key with its chosen models, then run its one pinned test.

    A key already stored for the provider is replaced, with ``models`` as its
    ordered list; each is checked against the provider's list for this key
    before anything is stored. With no model named, a terminal is asked
    (``ask_model``) and any other caller is refused (OMN-21059). The test is
    recorded and returned whether it passed or failed: the key stays stored
    either way, as ``onex models add`` has always left it.
    """
    # Credentials-page updates an earlier change could not apply go first, in order.
    drain_pending_credential_events(LocalByokCredentialStore().db_path)
    if len(set(models)) != len(models):
        raise ModelKeyRefusedError("a model is named twice. Nothing was stored.")
    plan = resolve_local_byok_credential_plan(provider)
    for extra in models[1:]:
        check_chosen_model(provider, plan, key, extra, echo)
    store_key(
        f"llm.{provider}.api_key",
        key,
        force=True,
        model_option=models[0] if models else None,
        ask_model=ask_model,
        echo=echo,
    )
    handler = model_handler()
    if len(models) > 1:
        try:
            listed = handler.handle(
                ModelModelSetupRequest(
                    operation="add_models",
                    provider=cast(ModelProvider, provider),
                    models=tuple(models[1:]),
                )
            )
        except ModelSetupRefusedError as refusal:
            raise ModelKeyRefusedError(f"{refusal}. Nothing was changed.") from None
        label = MODEL_LABELS[provider]
        echo(f"Your {label} models, first tried first: {', '.join(listed.models)}")
    (result,) = handler.handle(
        ModelModelSetupRequest(operation="test", provider=provider)
    ).tests
    return result


def list_providers() -> tuple[ModelModelStatus, ...]:
    """Each provider: set up or not, its models and its last test result."""
    return model_handler().handle(ModelModelSetupRequest(operation="status")).providers


def _usable(status: ModelModelStatus) -> bool:
    """Set up, and its last pinned test passed (OMN-20816 decision (a))."""
    return (
        status.set_up
        and status.last_test is not None
        and status.last_test.status == "passed"
    )


def refuse_last_usable(provider: str) -> None:
    """Refuse removing ``provider`` when no other provider would be left usable.

    Delegation needs at least one model it can use, so a removal that would
    leave none is refused before anything changes, in the terminal and for any
    other caller of :func:`remove_key` (OMN-20816 decision (c)).
    """
    statuses = list_providers()
    if not any(s.provider == provider and _usable(s) for s in statuses):
        return
    if any(s.provider != provider and _usable(s) for s in statuses):
        return
    label = MODEL_LABELS.get(provider, provider)
    raise LastUsableModelRefusedError(
        f"{label} is the last model delegation can use, and at least one is "
        "required. Add another and let its test pass first (onex models add "
        "<provider>), then remove this one. Nothing was changed."
    )


def remove_key(provider: str, echo: Echo = _quiet) -> None:
    """Delete ``provider``'s stored key, unless it is the last usable model.

    Keys only: Ollama's routes and a local URL are removed in the terminal, as
    before, and the guard does not cover them.
    """
    handler = model_handler()
    if provider not in KEYED_PROVIDERS:
        raise ModelKeyRefusedError(
            f"{MODEL_LABELS.get(provider, provider)} has no key to remove."
        )
    if not handler.is_set_up(provider):
        raise ModelKeyRefusedError(f"no {MODEL_LABELS[provider]} key is stored.")
    with _removal_lock(LocalByokCredentialStore().db_path):
        refuse_last_usable(provider)
        _delete_key(f"llm.{provider}.api_key", echo)
        handler.handle(ModelModelSetupRequest(operation="forget", provider=provider))


__all__ = [
    "DASHBOARD_PROVIDERS",
    "KEYED_PROVIDERS",
    "LOCAL_HOW",
    "MODEL_LABELS",
    "OLLAMA_HOW",
    "PENDING_CREDENTIAL_EVENTS_NAME",
    "AskModel",
    "Echo",
    "LastUsableModelRefusedError",
    "ModelKeyRefusedError",
    "add_key",
    "check_chosen_model",
    "delete_key",
    "drain_pending_credential_events",
    "fold_credential_events",
    "list_providers",
    "model_chosen_by",
    "model_handler",
    "refuse_last_usable",
    "remove_key",
    "resolve_model",
    "resolve_plan",
    "store_key",
]
