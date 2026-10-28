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
import contextlib
import inspect
import json
import os
import socket
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from getpass import getpass
from pathlib import Path
from typing import Any, Literal, cast

import click
import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr, ValidationError

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    local_credential_registered_at,
    resolve_local_byok_credential_plan,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.handlers.handler_captured_secret_resolve import (
    CapturedSecretReaderNotConfiguredError,
    HandlerCapturedSecretResolve,
    reader_for_this_machine,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_request import (
    ModelCapturedSecretResolveRequest,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_result import (
    EnumCapturedSecretResolveOutcome,
)
from omnimarket.nodes.node_captured_secret_store_effect.handlers.handler_captured_secret_store import (
    HandlerCapturedSecretStore,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_request import (
    ModelCapturedSecretStoreRequest,
)
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    LOCAL_URL_PROVIDER,
    OVERLAY_DEFAULT_PROVIDERS,
    PROVIDERS,
    HandlerModelSetup,
    ModelSetupRefusedError,
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
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreRequest,
    ModelSecretStoreResult,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    CredentialPlanUndeterminedError,
    CredentialStoreError,
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
from omnimarket.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
    resolve_byok_provider_backend,
)
from omnimarket.routing.local_byok_route import house_provider_slug

__all__ = ["delete_secret_value", "models_group", "secret_group", "store_secret_value"]

#: The leading characters a key for a known provider starts with. Checked only
#: on the terminal prompt, where a mistyped or mis-pasted value is the failure;
#: a piped value is a script's choice and is stored as given. A provider absent
#: here has no documented fixed prefix, so nothing is checked for it.
_KNOWN_KEY_PREFIXES: dict[str, str] = {
    "openrouter": "sk-or-v1-",
    "anthropic": "sk-ant-",
}


def _refused(error: model_keys.ModelKeyRefusedError) -> click.ClickException:
    """The shared path's refusal as this command's error. Its text is unchanged."""
    return click.ClickException(str(error))


def _check_chosen_model(
    provider: str, plan: str | None, value: str, chosen: str
) -> str:
    """Check the customer's model against their key's own list (OMN-20844)."""
    try:
        return model_keys.check_chosen_model(provider, plan, value, chosen, click.echo)
    except model_keys.ModelKeyRefusedError as refusal:
        raise _refused(refusal) from None


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


#: OMN-20994. The secret store node's contract maps this reference namespace to
#: the tenant folder, so this command holds a reference and never a folder.
_TENANT_REFERENCE_PREFIX = "secret://onex/tenant/"


class CentralStoreRefusedError(Exception):
    """The secret store node did not create the key. Carries no value."""

    def __init__(self, result: ModelSecretStoreResult) -> None:
        detail = f" ({result.detail})" if result.detail else ""
        super().__init__(
            f"the central secret store answered {result.outcome.value}{detail}"
        )
        self.outcome = result.outcome


def _central_secret_handler() -> HandlerSecretStore:
    """The secret store node's handler, store and identity from this machine's overlay."""
    return HandlerSecretStore()


class _CentralTenantKeyStore:
    """``ProtocolSecretStore`` over the secret store node's handler (OMN-20994).

    A tenant key is written in process, create-only, under the minted
    reference in the tenant namespace. Anything but ``created`` raises
    :class:`CentralStoreRefusedError`, so a refusal stops the registration
    before any event is published.
    """

    def __init__(self, handler: HandlerSecretStore) -> None:
        self._handler = handler

    async def get_secret(self, key: str) -> str | None:
        result = await self._handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.RESOLVE,
                reference=f"{_TENANT_REFERENCE_PREFIX}{key}",
            )
        )
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return None
        if result.value is None:
            raise CentralStoreRefusedError(result)
        return result.value.get_secret_value()

    async def set_secret(self, key: str, value: str) -> bool:
        result = await self._handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.CREATE,
                reference=f"{_TENANT_REFERENCE_PREFIX}{key}",
                value=SecretStr(value),
            )
        )
        if result.outcome is not EnumSecretStoreOutcome.CREATED:
            raise CentralStoreRefusedError(result)
        return True

    async def delete_secret(self, key: str) -> bool:
        raise PermissionError("the central secret store is written create-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        raise PermissionError("the central tenant store is not listed from here")

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _tenant_key_event_bus() -> ProtocolCredentialEventBus | None:
    """Let the publisher construct and own this runtime's Settings-backed bus.

    Tests may return an injected bus; production passes None so bootstrap and
    producer lifecycle follow the same path as hosted credential intake.
    """
    return None


def _environment_names_broker() -> bool:
    """Whether KAFKA_BOOTSTRAP_SERVERS (or KAFKA_BROKER) names a broker here."""
    from omnimarket.config.settings import Settings

    return bool(Settings().get_effective_kafka_bootstrap_servers())


def _lab_host_bus_lane(bound_host: str | None) -> str | None:
    """The bound host's declared ``ledger_bus_lane`` in the lab host table (OMN-20910).

    A lab host carries no KAFKA_BOOTSTRAP_SERVERS, but its lab host table row
    names the bus lane it publishes on; the lane's broker and login come from
    the lane declaration, never from the shell.
    """
    table = _lab_host_table()
    if table is None or bound_host is None:
        return None
    try:
        document = yaml.safe_load(table.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    rows = document.get("hosts") if isinstance(document, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == bound_host:
            lane = row.get("ledger_bus_lane")
            return lane.strip() if isinstance(lane, str) and lane.strip() else None
    return None


async def _register_on_declared_bus[T](
    register: Callable[[ProtocolCredentialEventBus | None], Awaitable[T]],
    *,
    lane: str | None,
) -> T:
    """Run ``register`` on the injected, environment or declared-lane bus (OMN-20910).

    An injected bus (tests) or a broker named by the environment keeps the
    publisher's own Settings-backed path. Otherwise the declared lane is opened
    through the lab-run lane resolver, which reads the broker and its login from
    the lane declaration. With neither, the publisher's refusal stands.
    """
    injected = _tenant_key_event_bus()
    if injected is not None or lane is None or _environment_names_broker():
        return await register(injected)
    from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        open_lab_run_bus,
    )

    workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
    # The opener's fourth keyword (the workspace root) carries the private
    # registry clone's name, which the public-repo hygiene gate refuses; its
    # parameters are bound from its signature in declared order, as
    # ``onex scm`` binds them.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise click.ClickException(f"unexpected bus opener parameters: {names}")
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    values = ("kafka", lane, None, Path(workspace) if workspace else None)
    try:
        async with opener(**dict(zip(names, values, strict=True))) as bus:
            return await register(cast(ProtocolCredentialEventBus, bus))
    except LabRunBusError as exc:
        raise click.ClickException(
            f"the declared bus lane {lane!r} could not be opened ({exc}); set "
            "KAFKA_BOOTSTRAP_SERVERS or fix the host's ledger_bus_lane."
        ) from exc


#: OMN-21012. The lab host table is a deployment fact kept outside this
#: repository, named by ONEX_LAB_RUN_HOSTS (the variable ``onex delegate`` and
#: the lab placement read). Without it this machine is off the lab and a key
#: is registered with no host binding.
_LAB_HOSTS_ENV = "ONEX_LAB_RUN_HOSTS"
#: A remote lane declares its host because its namespace may not carry the
#: host's own name (the same two variables ``onex delegate`` reads).
_LANE_HOST_ENVS = ("ONEX_LANE_HOST", "ONEX_REMOTE_LANE_HOST")


def _lab_host_table() -> Path | None:
    """The lab host table this machine reads, or ``None`` off the lab."""
    configured = os.environ.get(_LAB_HOSTS_ENV, "").strip()
    return Path(configured) if configured else None


def _lab_host_rows(table: Path) -> list[tuple[str, str]]:
    """Each row's ``(name, target)``, or a refusal naming the table."""
    try:
        document = yaml.safe_load(table.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(
            f"the lab host table {table} could not be read ({exc}); nothing was stored."
        ) from exc
    rows = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and row.get("name") for row in rows
    ):
        raise click.ClickException(
            f"the lab host table {table} has no hosts with a name; nothing was stored."
        )
    return [(str(row["name"]), str(row.get("target") or "")) for row in rows]


def _resolve_bound_host(declared: str | None) -> str | None:
    """The lab host a registered key is bound to (OMN-21012), or ``None``.

    Operator ruling 2026-10-11T17:58:45Z: each Claude account's key is bound to
    the machine the account lives on. ``--host`` names that machine and must be
    a row of the lab host table. Without it, the registering machine's own row
    is used: the declared lane host first, else the short hostname matched to a
    row's name or target. Off the lab (no table), or on a machine no row names,
    the key is unbound and usable from any host, as before.
    """
    table = _lab_host_table()
    if declared is not None:
        host = declared.strip()
        if table is None:
            raise click.ClickException(
                f"--host {declared!r} cannot be checked: there is no lab host table "
                f"on this machine (set {_LAB_HOSTS_ENV}); nothing was stored."
            )
        names = [name for name, _target in _lab_host_rows(table)]
        if host not in names:
            raise click.ClickException(
                f"--host {declared!r} is not a host in the lab host table "
                f"({', '.join(names)}); nothing was stored."
            )
        return host
    if table is None:
        return None
    rows = _lab_host_rows(table)
    row_names = {name for name, _target in rows}
    for env in _LANE_HOST_ENVS:
        lane_host = os.environ.get(env, "").strip()
        if lane_host:
            if lane_host not in row_names:
                raise click.ClickException(
                    f"{env}={lane_host!r} is not a host in the lab host table; "
                    "pass --host or fix the declaration. Nothing was stored."
                )
            return lane_host
    hostname = socket.gethostname().split(".")[0]
    matches = [name for name, target in rows if hostname in (name, target)]
    return matches[0] if len(matches) == 1 else None


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
@click.option(
    "--expires",
    default=None,
    metavar="YYYY-MM-DD",
    help=(
        "The date the key expires, as set where it was created (UTC). Company "
        "keys all expire on January 1; any other date is stored with a warning."
    ),
)
@click.option(
    "--host",
    default=None,
    help=(
        "The lab host whose Claude account owns this key (a lab host table row, "
        "e.g. h201). Work sent from that host is the only work that spends it. "
        "Defaults to this machine's row in the ONEX_LAB_RUN_HOSTS table; off "
        "the lab the key is unbound."
    ),
)
@click.option(
    "--workspace-id",
    "workspace_id",
    default=None,
    help=(
        "The Anthropic workspace the key belongs to (anthropic keys only). A "
        "company Claude API key is answered only when each request names its "
        "workspace, so the id is stored with the key and sent with every "
        "request made on it. It is an id, not a secret."
    ),
)
@click.option(
    "--store",
    "store_choice",
    type=click.Choice(["local", "central"]),
    default="local",
    show_default=True,
    help=(
        "Where the value is held. local: this machine's store. central: the "
        "shared tenant folder, written through the secret store node, so every "
        "lab host resolves it; this machine's store is left untouched."
    ),
)
def register_tenant_key(
    provider: str,
    tenant: str,
    name: str | None,
    plan: str | None,
    model_option: str | None,
    expires: str | None,
    host: str | None,
    workspace_id: str | None,
    store_choice: str,
) -> None:
    """Register a tenant's provider key from stdin and publish its reference."""
    if not tenant.strip():
        raise click.ClickException("--tenant must not be empty; nothing was stored.")
    expires_at = _parse_expires(expires)
    bound_host = _resolve_bound_host(host)
    central = store_choice == "central"
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
            expires_at=expires_at,
            bound_host=bound_host,
            workspace_id=workspace_id,
        )
        secret_store = (
            _CentralTenantKeyStore(_central_secret_handler())
            if central
            else _tenant_key_store()
        )
        response = asyncio.run(
            _register_on_declared_bus(
                lambda event_bus: register_inference_credential(
                    request,
                    tenant_id=tenant,
                    secret_store=secret_store,
                    event_bus=event_bus,
                ),
                lane=_lab_host_bus_lane(bound_host),
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
        # OMN-20991: a key the provider refuses is a typed refusal, not a
        # traceback. Its message names the provider's own words, never the key.
        CredentialKeyRefusedError,
        ByokPlanNotPermittedError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    except CentralStoreRefusedError as exc:
        raise click.ClickException(
            f"{exc}. Nothing was stored and nothing was published."
        ) from exc
    shown: dict[str, str | None] = {
        "api_key_ref": response.api_key_ref,
        "provider": response.provider,
        "plan": response.plan,
        "tenant": tenant,
    }
    if response.model is not None:
        shown["model"] = response.model
    if response.expires_at is not None:
        shown["expires_at"] = response.expires_at.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if response.expiry_warning is not None:
        shown["expiry_warning"] = response.expiry_warning.value
    if response.bound_host is not None:
        shown["bound_host"] = response.bound_host
    if response.workspace_id is not None:
        shown["workspace_id"] = response.workspace_id
    if not central:
        _fold_registered_tenant_key(response, tenant)
    click.echo(json.dumps(shown))


def _fold_registered_tenant_key(response: Any, tenant: str) -> None:
    """Catalogue the key on this machine too (OMN-21014).

    The value is in this machine's store; the credential-registered event goes
    to the bus for the central catalog. The host's own review gateway reads its
    keys from this machine's catalog, so the same row is folded into the local
    store the value lives beside, the way ``onex secret set`` folds its events.
    The fold is an idempotent upsert by reference; no value is written.
    """
    store = _tenant_key_store()
    payload: dict[str, object] = {
        "tenant_id": tenant,
        "provider": response.provider,
        "name": response.name,
        "api_key_ref": response.api_key_ref,
        "metadata": {},
        "expires_at": None
        if response.expires_at is None
        else response.expires_at.astimezone(UTC).isoformat(),
        "bound_host": response.bound_host,
        "workspace_id": response.workspace_id,
    }
    try:
        apply_credential_registered(payload, SqliteDatabaseAdapter(store.db_path))
    except sqlite3.Error as error:
        raise click.ClickException(
            f"the key was stored as {response.api_key_ref}, but this machine's "
            f"catalog could not record it ({type(error).__name__}); the review "
            "gateway on this host will not see it until it is registered again."
        ) from None


def _parse_expires(expires: str | None) -> datetime | None:
    """``--expires YYYY-MM-DD`` as midnight UTC that day, or a refusal.

    Read before the key, so a typo costs nothing: the value is never read from
    stdin and nothing is stored.
    """
    if expires is None:
        return None
    try:
        day = date.fromisoformat(expires.strip())
    except ValueError as exc:
        raise click.ClickException(
            f"--expires {expires!r} is not a date in the form YYYY-MM-DD; "
            "nothing was stored."
        ) from exc
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _drain_pending_credential_events(db_path: Path) -> None:
    """Apply Credentials-page updates an earlier command could not, then forget them."""
    try:
        model_keys.drain_pending_credential_events(db_path)
    except model_keys.ModelKeyRefusedError as refusal:
        raise _refused(refusal) from None


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

    The body of ``onex secret set`` once the value is in hand. It runs the path
    ``onex models add`` and the local dashboard share (OMN-20816); a terminal is
    asked for a model it was not given, and a script is refused.
    """
    try:
        model_keys.store_key(
            secret_ref,
            value,
            force=force,
            plan_option=plan_option,
            model_option=model_option,
            ask_model=_ask_model,
            echo=click.echo,
        )
    except model_keys.ModelKeyRefusedError as refusal:
        raise _refused(refusal) from None


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

    The body of ``onex secret delete``, shared with ``onex models remove`` and
    the local dashboard (OMN-20816).
    """
    try:
        model_keys.delete_key(secret_ref, click.echo)
    except model_keys.ModelKeyRefusedError as refusal:
        raise _refused(refusal) from None


@secret_group.command("store-captured")
@click.option(
    "--session",
    default="onex-secret-store-captured",
    show_default=True,
    help="Recorded as the stored secret's capturing session.",
)
def store_captured(session: str) -> None:
    """Store one value in the captured-secret namespace and print its reference.

    OMN-20926. The same write the content-capture producer makes for a secret
    its scrub detects, as the writer identity this machine's
    ``~/.onex/config.yaml`` names (block ``captured_secret_store``). The value
    is read from stdin, or a hidden prompt on a terminal, never from argv.
    Prints the outcome and the reference, never the value.
    """
    value = _read_value("the value to store")
    if not value:
        raise click.ClickException("no value was given; nothing was stored.")
    result = HandlerCapturedSecretStore(onex_home=Path.home() / ".onex").handle(
        ModelCapturedSecretStoreRequest(
            value=SecretStr(value), session_id=session, captured_at=datetime.now(UTC)
        )
    )
    click.echo(
        f"{result.outcome.value}: {result.reference or '-'}"
        + (f" ({result.detail})" if result.detail else "")
    )
    if result.reference is None:
        raise SystemExit(1)


@secret_group.command("check-captured")
@click.argument("reference")
@click.option(
    "--accessor",
    default="onex-secret-check-captured",
    show_default=True,
    help="Who is asking, for the access log. Not an authorization claim.",
)
def check_captured(reference: str, accessor: str) -> None:
    """Check that a captured secret REFERENCE resolves through the store.

    OMN-20926. Resolves as the reader identity this machine's
    ``~/.onex/config.yaml`` names (block ``captured_secret_store``), logged by
    the store and by the resolver. Prints the outcome and the store key, NEVER
    the value; exits non-zero unless the reference resolved.
    """
    try:
        request = ModelCapturedSecretResolveRequest(
            reference=reference, accessor=accessor
        )
        reader = reader_for_this_machine(Path.home() / ".onex")
    except (CapturedSecretReaderNotConfiguredError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from None
    except ModelOnexError as exc:  # a credentials.json refusal: names keys only
        raise click.ClickException(exc.message) from None
    result = HandlerCapturedSecretResolve(store=reader).handle(request)
    click.echo(
        f"{result.outcome.value}: {result.store_key or '-'}"
        + (f" ({result.detail})" if result.detail else "")
    )
    if result.outcome is not EnumCapturedSecretResolveOutcome.RESOLVED:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# ``onex models`` (OMN-20817): set up the models delegation can use. A shim like
# ``onex secret`` above: the key is stored by store_secret_value, and status and
# the pinned test delegation are node_model_setup_effect's.
# ---------------------------------------------------------------------------

_MODEL_LABELS = model_keys.MODEL_LABELS
_KEY_PAGES: dict[str, str] = {
    "gemini": "aistudio.google.com/apikey",
    "openrouter": "openrouter.ai/keys",
    "openai": "platform.openai.com/api-keys",
    "anthropic": "platform.claude.com/settings/keys",
    "glm": "z.ai/manage-apikey/apikey-list",
}
_OLLAMA_HOW = model_keys.OLLAMA_HOW
_LOCAL_HOW = model_keys.LOCAL_HOW
_NOT_ROUTED = "Delegation does not use this key until its test passes."


def _model_handler() -> HandlerModelSetup:
    return model_keys.model_handler()


def _show_model_test(result: ModelModelTestResult, as_json: bool) -> None:
    if as_json:
        click.echo(result.model_dump_json())
        return
    label = f"{_MODEL_LABELS[result.provider]:<11}"
    if result.status == "passed":
        click.echo(f"{label} ✓ answered ({result.model or result.backend_id})")
    elif result.status == "throttled":
        # OMN-21464: the provider answered 429. The key was not judged.
        throttled_on = f"{result.model}: " if result.model else ""
        click.echo(f"{label} ~ {throttled_on}{result.reason}")
        click.echo(
            "  The provider is throttling this model; that is not a verdict on "
            "your key. Delegation still uses it."
        )
        click.echo(f"  Try again shortly: onex models test {result.provider}")
    elif result.status == "failed":
        failed_on = f"{result.model}: " if result.model else ""
        click.echo(f"{label} ✗ {failed_on}{result.reason}")
        if result.provider != "ollama" and result.provider != LOCAL_URL_PROVIDER:
            # OMN-21259: a key that failed its test is kept but not routed.
            click.echo(f"  {_NOT_ROUTED}")
            click.echo(f"  Fix it, then run: onex models test {result.provider}")
    elif result.provider == "ollama":
        click.echo(f"{label} not set up. {_OLLAMA_HOW}")
    elif result.provider == LOCAL_URL_PROVIDER:
        click.echo(f"{label} not set up. {_LOCAL_HOW}")
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
        "Anthropic, GLM (z.ai), Ollama, or a local model server by URL.\n\nSet up any "
        "combination; delegation chooses among them for each task."
    ),
)


@models_group.command("add")
@click.argument("provider", type=_model_choice)
@click.option(
    "--model",
    "model_options",
    multiple=True,
    help=(
        "A model your key runs, checked against the provider's list for it; "
        "repeat it to choose several, first is tried first. You choose it for "
        "every provider: without --model a terminal is asked and a script is "
        "refused (onex models default-model names one from your key's list). "
        "For local, a model the server runs; repeat it for several, first "
        "tried first, each after the local models already declared."
    ),
)
@click.option(
    "--url",
    default=None,
    help=(
        "local only: the base URL of a model server that speaks the "
        "OpenAI-compatible API and needs no key, such as http://<host>:<port>."
    ),
)
@_model_json
def add_model(
    provider: str, model_options: tuple[str, ...], url: str | None, as_json: bool
) -> None:
    """Store PROVIDER's key, then test it with one delegation pinned to it.

    The key is read from stdin when piped, or asked for at a hidden prompt.
    A key already stored for PROVIDER is replaced, with the models named here
    as its ordered list. The test passes only when it answers on the first
    model stored for the key. Add models to a stored key later with
    ``onex models add-model``.

    ``local`` asks for no key: ``--url`` names a model server that speaks the
    OpenAI-compatible API and each ``--model`` a model it runs. The URL must
    answer before it is declared in the local routes, beside any routes already
    there. Local models are kept in order, first tried first, and a run tries
    them before any key; ``onex models reorder local`` and ``onex models remove
    local --model`` change the list.
    """
    name = provider.lower()
    handler = _model_handler()
    if url is not None and name != "local":
        raise click.ClickException(
            "--url is for a local model server: onex models add local --url "
            "<base url> --model <name>"
        )
    if name == "local":
        if url is None or not model_options:
            raise click.ClickException(_LOCAL_HOW)
        try:
            added = handler.handle(
                ModelModelSetupRequest(
                    operation="add_local", url=url, models=model_options
                )
            )
        except ModelSetupRefusedError as refusal:
            raise click.ClickException(str(refusal)) from None
        # The add ran one pinned test delegation per named model already.
        for result in added.tests:
            _show_model_test(result, as_json)
        if any(result.status != "passed" for result in added.tests):
            sys.exit(1)
        return
    if name == "ollama":
        if not handler.is_set_up("ollama"):
            raise click.ClickException(_OLLAMA_HOW)
        if model_options:
            raise click.ClickException(
                f"--model does not apply to Ollama. {_OLLAMA_HOW}"
            )
    else:
        if len(set(model_options)) != len(model_options):
            raise click.ClickException("a model is named twice. Nothing was stored.")
        key = _read_model_key(name)
        # OMN-20816: the shared path; the dashboard's GET /models lists what it stores.
        try:
            result = model_keys.add_key(
                name, key, model_options, ask_model=_ask_model, echo=click.echo
            )
        except model_keys.ModelKeyRefusedError as refusal:
            raise _refused(refusal) from None
        _show_model_test(result, as_json)
        if result.status != "passed":
            sys.exit(1)
        return
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
    for row in model_keys.list_providers():
        if as_json:
            click.echo(row.model_dump_json())
            continue
        label = f"{_MODEL_LABELS[row.provider]:<11}"
        last = row.last_test
        models = f" [models: {', '.join(row.models)}]" if row.models else ""
        if not row.set_up:
            click.echo(f"{label} not set up")
        elif last is None:
            click.echo(f"{label} set up, not tested yet{models}")
        elif last.status == "passed":
            click.echo(
                f"{label} set up, last test passed {last.tested_at} "
                f"({last.model}){models}"
            )
        else:
            failed_on = f" on {last.model}" if last.model else ""
            click.echo(
                f"{label} set up, last test FAILED {last.tested_at}{failed_on}: "
                f"{last.reason}{models}"
            )
            if (
                row.provider in _KEY_PAGES
            ):  # a keyed provider; Ollama and local have none
                click.echo(f"  {_NOT_ROUTED} Run: onex models test {row.provider}")


@models_group.command("default-model")
@click.argument("provider", type=_model_choice)
def default_model(provider: str) -> None:
    """Print the model onboarding registers for PROVIDER. Nothing is stored.

    OpenRouter's comes from the deployment overlay, and no key is read. Every
    other provider's key runs only models its owner chose (OMN-21059), so its
    default is read from the provider: the key (stdin when piped, else a hidden
    prompt) asks the provider which models it can use, and the best match among
    the catalogue's preferred families is printed. A model counts as chosen
    only when it is passed on. To use another model afterwards, one command:

        onex models add openrouter --model MODEL_ID
    """
    name = provider.lower()
    if name not in OVERLAY_DEFAULT_PROVIDERS:
        click.echo(_listed_default_model(name))
        return
    request = ModelModelSetupRequest(
        operation="default_model", provider=cast(ModelProvider, name)
    )
    try:
        result = _model_handler().handle(request)
    except ModelSetupRefusedError as refusal:
        raise click.ClickException(str(refusal)) from None
    click.echo(result.models[0])


def _listed_default_model(provider: str) -> str:
    """The best match on the key's own model list, or a refusal naming the way out."""
    choose = f"Name one with onex models add {provider} --model <model id>"
    backend = resolve_byok_provider_backend(provider)
    if backend is None:
        raise click.ClickException(
            f"{_MODEL_LABELS.get(provider, provider)} has no model list to read a "
            f"default from. {choose}"
        )
    discovery = discover_byok_model_sync(backend, _read_model_key(provider))
    refusal = describe_discovery_refusal(discovery)
    if refusal is not None:
        raise click.ClickException(refusal)
    if discovery.model is None:
        raise click.ClickException(
            f"could not read {_MODEL_LABELS[provider]}'s model list just now, so "
            f"no default can be named. {choose}"
        )
    return discovery.model


def _edit_model_list(
    handler: HandlerModelSetup, request: ModelModelSetupRequest
) -> None:
    """Run one list edit and say the resulting order, or the refusal."""
    try:
        result = handler.handle(request)
    except ModelSetupRefusedError as refusal:
        raise click.ClickException(f"{refusal}. Nothing was changed.") from None
    label = _MODEL_LABELS[cast(str, request.provider)]
    click.echo(f"Your {label} models, first tried first: {', '.join(result.models)}")


@models_group.command("add-model")
@click.argument("provider", type=_model_choice)
@click.option(
    "--model",
    "model_options",
    multiple=True,
    required=True,
    help="A model to add after the ones already chosen; repeat it for several.",
)
def add_model_to_key(provider: str, model_options: tuple[str, ...]) -> None:
    """Add models to PROVIDER's stored key, after the ones already chosen.

    Each model is checked against the provider's list for your key. The key is
    not asked for again.
    """
    name = provider.lower()
    if name == "ollama":
        raise click.ClickException(f"Ollama has no key to add models to. {_OLLAMA_HOW}")
    if name == "local":
        raise click.ClickException(
            "add a local model with its URL, after the ones already declared: "
            f"{_LOCAL_HOW}"
        )
    store = LocalByokCredentialStore()
    key = asyncio.run(store.get_secret(f"llm.{name}.api_key"))
    if key is None:
        raise click.ClickException(
            f"no {_MODEL_LABELS[name]} key is stored; add one with: "
            f"onex models add {name}"
        )
    plan = resolve_local_byok_credential_plan(name)
    for chosen in model_options:
        _check_chosen_model(name, plan, key, chosen)
    _edit_model_list(
        _model_handler(),
        ModelModelSetupRequest(
            operation="add_models",
            provider=cast(ModelProvider, name),
            models=tuple(model_options),
        ),
    )


@models_group.command("reorder")
@click.argument("provider", type=_model_choice)
@click.argument("models", nargs=-1, required=True)
def reorder_models(provider: str, models: tuple[str, ...]) -> None:
    """Put PROVIDER's chosen MODELS in this order, first tried first.

    Name exactly the models the key holds, each once. Nothing changes otherwise.
    """
    _edit_model_list(
        _model_handler(),
        ModelModelSetupRequest(
            operation="reorder",
            provider=cast(ModelProvider, provider.lower()),
            models=models,
        ),
    )


@models_group.command("remove")
@click.argument("provider", type=_model_choice)
@click.option(
    "--model",
    "model_options",
    multiple=True,
    help="Remove only this model from the key's list; the key stays.",
)
def remove_model(provider: str, model_options: tuple[str, ...]) -> None:
    """Delete PROVIDER's stored key, or with --model just those models from its list."""
    name = provider.lower()
    handler = _model_handler()
    if model_options:
        _edit_model_list(
            handler,
            ModelModelSetupRequest(
                operation="remove_model",
                provider=cast(ModelProvider, name),
                models=model_options,
            ),
        )
        return
    if name == "ollama":
        raise click.ClickException(
            f"Ollama's routes are in {handler.overrides_path()}; delete that file "
            "to stop using Ollama, and quit the Ollama app."
        )
    if name == "local":
        try:
            handler.handle(ModelModelSetupRequest(operation="remove_local"))
        except ModelSetupRefusedError as refusal:
            raise click.ClickException(str(refusal)) from None
        click.echo(f"Removed the local model URL from {handler.overrides_path()}")
        return
    # OMN-20816: the shared remove, which refuses the last usable provider here
    # as it does on the local dashboard.
    try:
        model_keys.remove_key(name, click.echo)
    except model_keys.ModelKeyRefusedError as refusal:
        raise _refused(refusal) from None


#: OMN-21014: the review a dry run plans. It carries no sampling fields: the
#: plan shows the gateway's own temperature and default max_tokens.
_REVIEW_GATEWAY_DRY_RUN_PAYLOAD: dict[str, object] = {
    "messages": [
        {"role": "system", "content": "You are a hostile code reviewer."},
        {"role": "user", "content": "Dry run: no diff is sent."},
    ],
}


@models_group.command("review-gateway")
@click.option(
    "--tenant",
    required=True,
    help="The tenant whose catalogued keys the gateway spends.",
)
@click.option(
    "--model",
    "model_option",
    required=True,
    help="The Claude model every review runs on, e.g. claude-sonnet-5-5.",
)
@click.option(
    "--thinking-mode",
    type=click.Choice(["disabled", "adaptive"]),
    default="disabled",
    show_default=True,
    help="Native thinking protocol declared by this gateway's host overlay.",
)
@click.option(
    "--host",
    default=None,
    help=(
        "This machine's lab host table row (ONEX_LAB_RUN_HOSTS); only keys bound "
        "to it are spent. Defaults to this machine's row."
    ),
)
@click.option(
    "--bind",
    default="127.0.0.1:8140",
    show_default=True,
    help="host:port the voter endpoint listens on.",
)
@click.option(
    "--privacy-overlay",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    envvar="ONEX_REVIEW_GATEWAY_PRIVACY_OVERLAY",
    required=True,
    help=(
        "A YAML file supplied by whoever runs the gateway, whose "
        "cloud_review_refused_repositories list names the repositories whose "
        "diffs never go to a cloud model. The gateway refuses to start without "
        "a non-empty list."
    ),
)
@click.option(
    "--repository",
    default=None,
    help="The review's metadata.repository, used only by --dry-run.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Plan one review (the key it would use, the body it would send) and exit.",
)
def review_gateway(
    tenant: str,
    model_option: str,
    thinking_mode: Literal["disabled", "adaptive"],
    host: str | None,
    bind: str,
    privacy_overlay: Path,
    repository: str | None,
    dry_run: bool,
) -> None:
    """Serve a Hostile Review voter endpoint on this host's Claude keys (OMN-21014)."""
    from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
        GATEWAY_PLAN,
        HandlerClaudeReviewGateway,
        run_dry,
    )
    from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
        ModelClaudeReviewGatewayRequest,
    )

    try:
        overlay = yaml.safe_load(privacy_overlay.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise click.ClickException(
            "the privacy overlay must be a readable YAML mapping."
        ) from None
    refused = (
        overlay.get("cloud_review_refused_repositories")
        if isinstance(overlay, Mapping)
        else None
    )
    if (
        not isinstance(refused, list)
        or not refused
        or any(not isinstance(name, str) or not name.strip() for name in refused)
    ):
        raise click.ClickException(
            "the privacy overlay must be a YAML mapping with a non-empty "
            "cloud_review_refused_repositories list of non-empty strings."
        )
    cloud_refused_repositories = tuple(name.strip() for name in refused)

    bound_host = _resolve_bound_host(host)
    if bound_host is None:
        raise click.ClickException(
            "the review gateway needs this machine's lab host: pass --host or set "
            "ONEX_LAB_RUN_HOSTS to the lab host table."
        )
    bind_host, _, bind_port = bind.rpartition(":")
    # OMN-20910: the gateway names the native Messages plan explicitly; a caller
    # that names no plan keeps the OpenAI-compatible default row.
    backend = resolve_byok_provider_backend("anthropic", GATEWAY_PLAN)
    if backend is None:
        raise click.ClickException(
            "the provider catalogue offers no anthropic api_native backend."
        )
    try:
        request = ModelClaudeReviewGatewayRequest(
            host=bound_host,
            tenant_id=tenant,
            model=model_option,
            thinking_mode=thinking_mode,
            cloud_refused_repositories=cloud_refused_repositories,
            bind_host=bind_host or "127.0.0.1",
            bind_port=int(bind_port),
            dry_run=dry_run,
        )
    except (ValidationError, ValueError) as exc:
        raise click.ClickException(
            f"review-gateway refused its options: {exc}"
        ) from None
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
        HandlerLlmDelegationCall,
    )

    store = _tenant_key_store()
    handler = HandlerClaudeReviewGateway(
        catalog=SqliteDatabaseAdapter(store.db_path),
        key_values=store,
        # The one sanctioned model caller (OMN-20295); it resolves the key's
        # value through the secret store at its own boundary.
        call=HandlerLlmDelegationCall(),
        endpoint_url=backend.endpoint_url,
    )
    if dry_run:
        payload = _REVIEW_GATEWAY_DRY_RUN_PAYLOAD.copy()
        if repository is not None:
            payload["metadata"] = {"repository": repository}
        plan = run_dry(handler, request, payload)
        click.echo(json.dumps(plan.model_dump(mode="json"), sort_keys=True))
        return
    click.echo(
        f"onex models review-gateway: http://{request.bind_host}:{request.bind_port}"
        f"/v1/chat/completions (host {bound_host}, model {model_option}, "
        f"{len(cloud_refused_repositories)} repositories refused cloud review)"
    )
    asyncio.run(handler.handle(request))
