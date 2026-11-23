# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant`` -- the operator surface that creates a tenant and mints and revokes its gateway key (OMN-26475).

    onex tenant create --slug acme --name "Acme"
    onex tenant show --slug acme
    onex tenant key mint --tenant acme --name ci-key [--expires-in 72h] [--store-dev-secret]
    onex tenant key revoke --tenant acme --key-id <uuid>

HOW THIS COMMAND REACHES THE CLI
    It is not wired in by hand anywhere. ``omnimarket``'s ``pyproject.toml`` advertises ``tenant`` in the ``onex.cli``
    entry-point group and ``omnibase_core.cli.cli_commands`` discovers that group over the installed distributions.

HOW EACH COMMAND REACHES A NODE
    Every command is one operation of a node and goes through ``_dispatch``, which calls omnibase_core's public
    ``publish_and_poll`` with that operation's explicit ``-requested`` and ``-completed`` topics, read from the owning
    node's ``contract.yaml`` by operation name. No topic is named in this file. ``onex run-node`` is not used: it
    reaches one operation per node. The broker is ``ModelNodeBrokerBinding.for_node(node_id)``. Every answer, a refusal
    included, arrives on ``-completed``, so a timeout means only that no answer came.

THE KEY NEVER TRAVELS ON THE BUS
    ``key mint`` generates the plaintext in this process and sends only its sha256 digest. With ``--store-dev-secret`` it
    writes the plaintext create-only to the secret store's ``gateway_key`` folder, and only when the command lane, the
    api-key node's overlay lane and the store's environment all say dev. Without it the plaintext is printed once.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import click
import yaml
from omnibase_core.cli.cli_run_node import publish_and_poll
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.event_bus.model_node_broker_binding import (
    ModelNodeBrokerBinding,
)
from omnibase_core.models.event_bus.model_node_broker_resolution_error import (
    ModelNodeBrokerResolutionError,
)
from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
    load_secret_store_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreRequest,
)
from omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_gateway_overlay import (
    ModelWorkflowGatewayOverlay,
)

_PROVISION_NODE: Final = "node_tenant_provision_effect"
_API_KEY_NODE: Final = "node_tenant_api_key_effect"
_GATEWAY_NODE: Final = "node_workflow_gateway_effect"

_OP_CREATE: Final = "tenant_provision"
_OP_READ: Final = "tenant_read"
_OP_REGISTER: Final = "tenant_api_key_register_requested"
_OP_REVOKE: Final = "tenant_api_key_revoke_requested"

_KEY_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EXPIRES_IN = re.compile(r"^(\d+)([mhd])$")
_EXPIRES_UNITS: Final = {"m": "minutes", "h": "hours", "d": "days"}

_DONE_CREATE: Final = frozenset({"created", "already_exists"})
_DONE_READ: Final = frozenset({"found"})
_DONE_REGISTER: Final = frozenset({"registered", "already_registered"})
_DONE_REVOKE: Final = frozenset({"revoked", "already_revoked"})

_REFUSED_NOT_DEV: Final = "refused_not_dev"
_REFUSED_STORE_NOT_DEV: Final = "refused_store_not_dev"


def _node_dir(node_id: str) -> Path:
    module = importlib.import_module(f"omnimarket.nodes.{node_id}")
    return Path(str(module.__file__)).resolve().parent


def _contract(node_id: str) -> dict[str, Any]:
    document = yaml.safe_load(
        (_node_dir(node_id) / "contract.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(document, dict):
        raise click.ClickException(f"{node_id}: contract.yaml is not a mapping")
    return document


def _operation_topics(node_id: str, operation: str) -> tuple[str, str]:
    """The ``-requested`` and ``-completed`` topic of one operation, read from the node's contract by operation name.

    A contract that declares a ``topics`` block names both per operation. One that does not names the operation's
    command topic in ``handler_routing`` and the answer is the command's ``-completed`` pair, which must be one of the
    contract's own ``publish_topics``.
    """
    contract = _contract(node_id)
    declared = contract.get("topics")
    if isinstance(declared, dict) and operation in declared:
        pair = declared[operation]
        return str(pair["requested"]), str(pair["completed"])
    handlers = contract.get("handler_routing", {}).get("handlers", [])
    requested = next(
        (str(h["topic"]) for h in handlers if h.get("operation") == operation), None
    )
    if requested is None:
        raise click.ClickException(
            f"{node_id}: contract.yaml declares no operation {operation!r}"
        )
    completed = requested.replace(".cmd.", ".evt.").replace(
        "-requested.", "-completed."
    )
    published = contract.get("event_bus", {}).get("publish_topics", [])
    if completed not in published:
        raise click.ClickException(
            f"{node_id}: contract.yaml publishes no completed topic for {operation!r}"
        )
    return requested, completed


def _dispatch(
    node_id: str, operation: str, payload: dict[str, object], timeout: int
) -> dict[str, Any] | None:
    """One command to one operation of a node; the answer's payload, or ``None`` when no answer came in time."""
    requested, completed = _operation_topics(node_id, operation)
    try:
        binding = ModelNodeBrokerBinding.for_node(node_id)
    except ModelNodeBrokerResolutionError as exc:
        raise click.ClickException(str(exc)) from None
    if not binding.bus_type.requires_broker or not binding.bootstrap_servers:
        raise click.ClickException(
            f"the {node_id} overlay {binding.overlay_path} declares a bus with no broker; "
            "`onex tenant` runs against a lane's broker only"
        )
    try:
        response = publish_and_poll(
            node_id,
            payload,
            timeout,
            binding.bootstrap_servers,
            requested,
            completed,
            client_security=binding.client_security() or None,
        )
    except ModelOnexError as exc:
        raise click.ClickException(exc.message) from None
    if response is None:
        return None
    body = response.get("payload", response)
    if not isinstance(body, dict) or "outcome" not in body:
        raise click.ClickException(
            f"{operation}: the answer on {completed} carries no outcome"
        )
    return dict(body)


def _call(
    node_id: str, operation: str, payload: dict[str, object], timeout: int
) -> dict[str, Any]:
    answer = _dispatch(node_id, operation, payload, timeout)
    if answer is None:
        raise click.ClickException(
            f"no answer within {timeout}s for {operation}; nothing is known about whether it ran"
        )
    return answer


def _require(answer: dict[str, Any], done: frozenset[str]) -> None:
    outcome = str(answer["outcome"])
    if outcome not in done:
        detail = answer.get("detail")
        raise click.ClickException(f"{outcome}: {detail}" if detail else outcome)


def _show(answer: dict[str, Any]) -> None:
    click.echo(json.dumps(answer, indent=2, sort_keys=True, default=str))


def _timeout_option(function: Any) -> Any:
    return click.option(
        "--timeout",
        type=int,
        default=30,
        show_default=True,
        help="Seconds to wait for each answer.",
    )(function)


def _tenant_ref(tenant: str) -> dict[str, object]:
    try:
        return {"tenant_id": str(UUID(tenant))}
    except ValueError:
        return {"tenant_slug": tenant}


def _resolve_tenant(tenant: str, timeout: int) -> tuple[UUID, str, str]:
    """The tenant's id, slug and provisioning state, read on the read operation's own topic pair."""
    answer = _call(
        _PROVISION_NODE,
        _OP_READ,
        {"command_id": str(uuid4()), **_tenant_ref(tenant)},
        timeout,
    )
    _require(answer, _DONE_READ)
    return (
        UUID(str(answer["tenant_id"])),
        str(answer["tenant_slug"]),
        str(answer["provisioning_state"]),
    )


tenant_group = click.Group(
    "tenant",
    help="Create a tenant, show it, and register and revoke its gateway key over the bus.",
)


@tenant_group.command("create")
@click.option("--slug", required=True, help="The tenant slug.")
@click.option("--name", required=True, help="The tenant's display name.")
@_timeout_option
def create_command(slug: str, name: str, timeout: int) -> None:
    """Create one tenant."""
    answer = _call(
        _PROVISION_NODE,
        _OP_CREATE,
        {"command_id": str(uuid4()), "tenant_slug": slug, "name": name},
        timeout,
    )
    _require(answer, _DONE_CREATE)
    _show(answer)


@tenant_group.command("show")
@click.option("--id", "tenant_id", default=None, help="The tenant id.")
@click.option("--slug", default=None, help="The tenant slug.")
@_timeout_option
def show_command(tenant_id: str | None, slug: str | None, timeout: int) -> None:
    """Show one tenant, by id or by slug."""
    if (tenant_id is None) == (slug is None):
        raise click.UsageError("name exactly one of --id and --slug")
    answer = _call(
        _PROVISION_NODE,
        _OP_READ,
        {
            "command_id": str(uuid4()),
            **(
                {"tenant_id": tenant_id}
                if tenant_id is not None
                else {"tenant_slug": slug}
            ),
        },
        timeout,
    )
    _require(answer, _DONE_READ)
    _show(answer)


key_group = click.Group("key", help="Register and revoke a tenant's gateway keys.")
tenant_group.add_command(key_group)


def _expires_in(_ctx: click.Context, _param: click.Parameter, value: str) -> timedelta:
    match = _EXPIRES_IN.fullmatch(value)
    if match is None or int(match.group(1)) < 1:
        raise click.BadParameter("a duration such as 30m, 72h or 7d")
    return timedelta(**{_EXPIRES_UNITS[match.group(2)]: int(match.group(1))})


def _key_name(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    if _KEY_NAME.fullmatch(value) is None:
        raise click.BadParameter(
            "lower-case letters, digits and hyphens, starting with a letter or digit, at most 63 characters"
        )
    return value


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def _load_store_overlay() -> ModelSecretStoreOverlay | None:
    """This machine's ``secret_store`` overlay block, or ``None`` when it has none or it is unreadable."""
    from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles
    from omnibase_infra.doctor.delegation_doctor_support import default_onex_home

    files = StoreOnexHomeFiles(default_onex_home())
    try:
        return load_secret_store_overlay(
            files.load_config(must_exist=False), source=str(files.config_path)
        )
    except (ModelOnexError, ValueError):
        return None


def _make_secret_handler(overlay: ModelSecretStoreOverlay) -> HandlerSecretStore:
    return HandlerSecretStore(overlay=overlay)


def _refuse(outcome: str, detail: str) -> click.ClickException:
    return click.ClickException(f"{outcome}: {detail}")


def _dev_store_guard(lane: str | None) -> ModelSecretStoreOverlay:
    """Refuse a dev secret before any store or bus call unless lane, overlay lane and store environment all say dev."""
    declared: str = _contract(_API_KEY_NODE)["dev_only"]["lane"]
    command_lane = lane or os.environ.get("ONEX_LANE") or None
    if command_lane != declared:
        raise _refuse(
            _REFUSED_NOT_DEV,
            f"command lane {command_lane!r} is not the declared dev lane {declared!r}",
        )
    try:
        node_overlay = load_structured_overlay(_API_KEY_NODE, ModelTenantApiKeyOverlay)
    except NodeOverlayError as exc:
        raise _refuse(
            _REFUSED_NOT_DEV, f"the {_API_KEY_NODE} overlay is unreadable: {exc}"
        ) from None
    if node_overlay.lane != declared:
        raise _refuse(
            _REFUSED_NOT_DEV,
            f"overlay lane {node_overlay.lane!r} is not the declared dev lane {declared!r}",
        )
    expected = node_overlay.dev_secret_store_environment
    store_overlay = _load_store_overlay()
    if expected is None:
        raise _refuse(
            _REFUSED_STORE_NOT_DEV,
            f"the {_API_KEY_NODE} overlay names no dev_secret_store_environment",
        )
    if (
        store_overlay is None
        or store_overlay.environment is None
        or store_overlay.environment != expected
    ):
        raise _refuse(
            _REFUSED_STORE_NOT_DEV,
            "this machine's secret store is not the dev store: its environment is unset or differs from the "
            "overlay's dev_secret_store_environment",
        )
    return store_overlay


def _gateway_key_prefix() -> str:
    try:
        overlay = load_structured_overlay(_GATEWAY_NODE, ModelWorkflowGatewayOverlay)
    except NodeOverlayError as exc:
        raise click.ClickException(
            f"the {_GATEWAY_NODE} overlay is unreadable: {exc}"
        ) from None
    return overlay.api_key.prefix


def _store_key(
    overlay: ModelSecretStoreOverlay, store_name: str, plaintext: str
) -> str:
    """Write the key create-only to the ``gateway_key`` folder; the stored path, or a refusal naming the outcome."""
    handler = _make_secret_handler(overlay)
    prefix = service_endpoint_url("internal_targets.secret_reference_prefix")
    result = asyncio.run(
        handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.CREATE,
                reference=f"{prefix}gateway_key/{store_name}",
                value=SecretStr(plaintext),
            )
        )
    )
    if result.outcome is not EnumSecretStoreOutcome.CREATED:
        raise click.ClickException(
            f"the dev secret store answered {result.outcome.value}"
            + (f" ({result.detail})" if result.detail else "")
            + "; nothing was registered"
        )
    return f"{result.folder}/{result.key}"


@key_group.command("mint")
@click.option("--tenant", required=True, help="The tenant, by slug or id.")
@click.option("--name", required=True, callback=_key_name, help="The key's name.")
@click.option(
    "--expires-in",
    "expires_in",
    default="72h",
    show_default=True,
    callback=_expires_in,
    help="How long the key lives: 30m, 72h, 7d.",
)
@click.option(
    "--store-dev-secret",
    is_flag=True,
    help="Write the key create-only to the dev secret store instead of printing it (dev lane only).",
)
@click.option("--lane", default=None, help="The command lane (else ONEX_LANE).")
@_timeout_option
def mint_command(
    tenant: str,
    name: str,
    expires_in: timedelta,
    store_dev_secret: bool,
    lane: str | None,
    timeout: int,
) -> None:
    """Mint a gateway key: the key is made here, only its hash goes on the bus."""
    store_overlay = _dev_store_guard(lane) if store_dev_secret else None
    plaintext = _gateway_key_prefix() + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    tenant_id, slug, state = _resolve_tenant(tenant, timeout)
    if state != "active":
        raise click.ClickException(
            f"refused_tenant_not_active: tenant {slug} is {state}; nothing was stored or registered"
        )
    secret_path = (
        _store_key(store_overlay, f"{slug}.{name}.{key_hash[:8]}", plaintext)
        if store_overlay is not None
        else None
    )
    payload: dict[str, object] = {
        "correlation_id": str(uuid4()),
        "tenant_id": str(tenant_id),
        "name": name,
        "key_hash": key_hash,
        "expires_at": (datetime.now(UTC) + expires_in).isoformat(),
    }
    # The poll can lose the terminal event after the registration committed: resend the identical payload once.
    # Register dedups on name and hash, so a committed first send answers already_registered.
    answer = _dispatch(_API_KEY_NODE, _OP_REGISTER, payload, timeout)
    if answer is None:
        answer = _dispatch(_API_KEY_NODE, _OP_REGISTER, payload, timeout)
    orphan = (
        f" The stored secret {secret_path} is orphaned and inert: no hash row exists for it."
        if secret_path is not None
        else ""
    )
    if answer is None:
        stored = f" The secret is stored at {secret_path}." if secret_path else ""
        raise click.ClickException(
            f"indeterminate: no answer to the registration after a resend.{stored} Nothing is printed; "
            f"run `onex tenant key list --tenant {slug}` to see whether key {name} registered."
        )
    outcome = str(answer["outcome"])
    if outcome not in _DONE_REGISTER:
        detail = answer.get("detail")
        raise click.ClickException(
            outcome + (f": {detail}" if detail else "") + "." + orphan
        )
    click.echo(
        f"{outcome}: key {name} credential {answer.get('credential_id')} of tenant {slug}, "
        f"expires {answer.get('expires_at')}",
        err=True,
    )
    if secret_path is not None:
        click.echo(f"the key is in the dev secret store at {secret_path} and is usable")
        return
    if _stdout_is_tty() and not click.confirm(
        "Show the key now? It is not shown again", err=True
    ):
        click.echo(
            f"registered and never shown: revoke credential {answer.get('credential_id')} and mint again",
            err=True,
        )
        return
    click.echo(plaintext)


@key_group.command("revoke")
@click.option("--tenant", required=True, help="The tenant, by slug or id.")
@click.option("--key-id", required=True, type=click.UUID, help="The credential id.")
@_timeout_option
def revoke_command(tenant: str, key_id: UUID, timeout: int) -> None:
    """Revoke one gateway key of a tenant."""
    tenant_id, _slug, _state = _resolve_tenant(tenant, timeout)
    answer = _call(
        _API_KEY_NODE,
        _OP_REVOKE,
        {
            "correlation_id": str(uuid4()),
            "tenant_id": str(tenant_id),
            "credential_id": str(key_id),
        },
        timeout,
    )
    _require(answer, _DONE_REVOKE)
    _show(answer)


__all__: list[str] = ["tenant_group"]
