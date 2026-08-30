# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Secret-store resolution at the inference effect boundary (OMN-12824).

The routing authority carries only a *reference* to a secret (``api_key_ref``)
— the env-var / Infisical key name, never the value. The literal secret value
is resolved here, at the provider-call effect boundary, through the canonical
``ProtocolSecretStore`` (omnibase_spi). This replaces the direct
``os.environ[...]`` reads that previously coupled the handlers to environment
variables and bypassed the secret store.

Resolution is fail-closed: when a backend declares an ``api_key_ref`` but the
secret store has no value for it, resolution raises. Callers never substitute a
default and never fall back to an empty key.

Default store: ``AdapterEnvSecretStore`` (omnibase_infra) — reads from the
process environment. The same interface resolves from Infisical when an
Infisical-backed ``ProtocolSecretStore`` is injected (the canonical store at the
deployed effect boundary), so no handler code changes when the secret source
moves from env to Infisical.

Secret values are wrapped in ``SecretStr`` so they are never accidentally
printed, logged, or serialized.
"""

from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING, cast
from uuid import UUID

import yaml
from omnibase_infra.handlers.models.infisical import ModelInfisicalHandlerConfig
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from omnibase_infra.runtime.secret_resolver import SecretResolver
from omnibase_infra.secret_stores import AdapterEnvSecretStore
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr, ValidationError

if TYPE_CHECKING:
    from omnibase_infra.handlers.handler_infisical import HandlerInfisical

from omnimarket.tenant_credential_ref import (
    is_tenant_credential_ref,
    tenant_hint_from_ref,
)


class SecretResolutionError(RuntimeError):
    """Raised when a declared ``api_key_ref`` cannot be resolved fail-closed."""


class SecretStoreConfigurationError(RuntimeError):
    """Raised when a lane declares a secret source the store cannot construct.

    OMN-16984 AC2. Deliberately NOT a subclass of :class:`SecretResolutionError`:
    a missing VALUE and an unconstructable STORE are different facts, and the
    tenant-overlay wrapper rewrites ``SecretResolutionError`` into "the tenant
    must register this ref", which would misattribute a lane misconfiguration to
    the customer. This error propagates uncaught and names only variable /
    logical names -- never a secret value.
    """


class _MappedSecretStore:
    """``ProtocolSecretStore`` adapter over infra's logical secret resolver."""

    def __init__(self, resolver: SecretResolver) -> None:
        self._resolver = resolver

    async def get_secret(self, key: str) -> str | None:
        resolved = await self._resolver.get_secret_async(key, required=False)
        if resolved is None:
            return None
        return cast(str, resolved.get_secret_value())

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Mapped secret store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Mapped secret store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        return


# OMN-13960: provider-native env-var aliases for delegation secret refs.
# ~/.omnibase/.env carries PROVIDER-NATIVE secret names (``OPENROUTER_API_KEY``,
# ``GEMINI_API_KEY``) that do NOT match the dotted-ref → ``LLM_*_API_KEY``
# convention (``llm.openrouter.api_key`` → ``LLM_OPENROUTER_API_KEY``). OMN-13943
# threaded these as a per-CALL-SITE ``env_var_fallback`` (each backend's bifrost
# ``api_key_env``); this map accepts the SAME provider-native names at the STORE
# level so resolution succeeds regardless of whether a given call site remembered
# to thread ``api_key_env`` — the LLM-judge inference adapter did not, so an
# OpenRouter/Gemini-backed judge would fail-closed even though the key is present.
# The CANONICAL source of these names is ``bifrost_delegation.yaml`` ``api_key_env``;
# this store-level map is the resolver-side safety net, not a second source of
# truth. Fail-closed is preserved: this is the LAST lookup, so a genuinely-absent
# secret still resolves to ``None`` (and ``required=True`` callers still raise).
# OMN-16891: the OpenRouter alias was ``OPEN_ROUTER_API_KEY``. OMN-13943 and
# OMN-15048 introduced and then propagated that spelling on the stated premise
# that "canonical ~/.omnibase/.env declares OPEN_ROUTER_API_KEY (with
# underscore)". Live probe 2026-08-28 (names + LENGTHS only, no value read)
# falsifies it:
#   .201 host  ~/.omnibase/.env : OPENROUTER_API_KEY len 73, OPEN_ROUTER_* len 0
#   every deployed runtime lane : BOTH names len 0
# The lanes came up empty because omnibase_infra's per-lane
# ``secret_resolver_mappings`` named the underscored form with
# ``enable_convention_fallback: false`` — the ONLY resolution path on a lane —
# so the OpenRouter rung has never been able to authenticate anywhere. Both
# repos now converge on the provider's own documented spelling, the one the
# host actually exports. The retired name is DELETED rather than kept
# alongside: an alias naming a variable no surface defines reads as configured
# while resolving to nothing, which is the exact failure being closed here.
_PROVIDER_NATIVE_SECRET_ALIASES: dict[str, tuple[str, ...]] = {
    "llm.openrouter.api_key": ("OPENROUTER_API_KEY",),
    "llm.gemini.api_key": ("GEMINI_API_KEY",),
}


class _ConventionFallbackSecretStore:
    """Default local store: literal env lookup, then dotted-ref → ENV_VAR convention.

    OMN-13861: the bare ``AdapterEnvSecretStore`` resolves ``os.environ[key]``
    LITERALLY, so a dotted logical ref (``llm.glm.api_key``) never matched the
    canonical ``LLM_GLM_API_KEY`` env var and every authenticated cloud delegation
    call failed to resolve its key on the bus-less local path (no
    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` lane wired). This store composes both
    lookups and is a STRICT SUPERSET of the prior literal-only behavior:

      1. literal ``AdapterEnvSecretStore`` — a caller (or test fixture) that sets
         the exact ref name (incl. a literal dotted ``llm.glm.api_key``) still
         resolves, so nothing that worked before breaks;
      2. on a miss, the ``SecretResolver`` convention fallback maps the dotted ref
         to its canonical env-var name (``llm.glm.api_key`` → ``LLM_GLM_API_KEY``),
         so the real ``LLM_*_API_KEY`` env vars now resolve.

    A ref that is already an env-var-shaped name resolves identically in both
    lookups. Read-only, like the sibling stores.
    """

    def __init__(self) -> None:
        self._literal: ProtocolSecretStore = AdapterEnvSecretStore()
        self._convention: ProtocolSecretStore = _MappedSecretStore(
            SecretResolver(
                config=ModelSecretResolverConfig(enable_convention_fallback=True)
            )
        )

    async def get_secret(self, key: str) -> str | None:
        literal = await self._literal.get_secret(key)
        if literal:
            return literal
        convention = await self._convention.get_secret(key)
        if convention:
            return convention
        # OMN-13960: provider-native alias — accept the real ~/.omnibase/.env name
        # (e.g. ``OPEN_ROUTER_API_KEY``/``GEMINI_API_KEY``) when neither the literal
        # ref nor the dotted → ``LLM_*_API_KEY`` convention resolved. This makes
        # OpenRouter/Gemini secrets resolvable from ANY call site, including ones
        # that do not thread the bifrost ``api_key_env`` fallback (the judge path).
        for alias in _PROVIDER_NATIVE_SECRET_ALIASES.get(key, ()):
            value = os.environ.get(alias)
            if value:
                return value
        return None

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Convention-fallback secret store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Convention-fallback secret store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return await self._literal.list_keys(prefix)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        return


# OMN-16984: the Infisical machine-identity bootstrap. These are the SAME
# variables the intake side already reads in
# ``omnimarket.projection.credential_publisher._build_secret_store()`` -- this
# is the read half of the same credential plane, not a second configuration
# surface. A machine identity is the sanctioned bootstrap exception to
# "config comes from contracts": it is the credential that unlocks the store
# the contracts are resolved from, and a keyring cannot unlock itself.
_INFISICAL_BOOTSTRAP_VARS: tuple[str, ...] = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)

_TRUTHY = frozenset({"true", "1", "yes"})


def _infisical_required() -> bool:
    """Whether the lane declares itself Infisical-controlled.

    Mirrors ``runtime_host_process``'s reading of the same variable so a lane
    cannot be "controlled" for the config prefetcher and uncontrolled at the
    effect boundary.
    """
    raw = os.environ.get("INFISICAL_REQUIRED", "")  # ONEX_EXCLUDE: secret_resolver
    return raw.strip().lower() in _TRUTHY


def _infisical_folder(source_path: str) -> str:
    """Return the Infisical FOLDER a mapping's ``source_path`` addresses.

    ``SecretResolver._read_infisical_secret_sync`` keeps only the last path
    segment as the secret key and reads it through the handler's configured
    ``secret_path``, so the folder in a mapping is carried by the handler, not
    by the per-read call. Deriving it from the mapping keeps the rendered lane
    config the single authority for addressing -- no new env var.
    """
    raw = source_path.rsplit("#", 1)[0]
    if "/" not in raw:
        return "/"
    folder = raw.rsplit("/", 1)[0]
    return folder or "/"


def _infisical_bootstrap_config(secret_path: str) -> ModelInfisicalHandlerConfig:
    """Build the typed handler config from the lane's bootstrap env, or raise.

    Raises:
        SecretStoreConfigurationError: naming every missing/blank variable at
            once. Variable NAMES only; no value is ever interpolated.
    """
    values = {
        name: os.environ.get(name, "").strip()  # ONEX_EXCLUDE: secret_resolver
        for name in _INFISICAL_BOOTSTRAP_VARS
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise SecretStoreConfigurationError(
            "Lane declares an Infisical-backed secret source but the Infisical "
            "machine identity is not fully configured. Missing or blank: "
            f"{missing}. Declared bootstrap variables: "
            f"{list(_INFISICAL_BOOTSTRAP_VARS)}."
        )
    try:
        return ModelInfisicalHandlerConfig(
            host=values["INFISICAL_ADDR"],
            client_id=SecretStr(values["INFISICAL_CLIENT_ID"]),
            client_secret=SecretStr(values["INFISICAL_CLIENT_SECRET"]),
            project_id=UUID(values["INFISICAL_PROJECT_ID"]),
            environment_slug=values["INFISICAL_ENVIRONMENT_SLUG"],
            secret_path=secret_path,
        )
    except (ValidationError, ValueError) as exc:
        raise SecretStoreConfigurationError(
            "Infisical machine-identity bootstrap failed validation "
            f"(secret_path={secret_path!r}); check "
            f"{list(_INFISICAL_BOOTSTRAP_VARS)}. Underlying error type: "
            f"{type(exc).__name__}"
        ) from exc


def _build_infisical_handler(config: ModelInfisicalHandlerConfig) -> HandlerInfisical:
    """Construct and initialize ``HandlerInfisical`` for this lane.

    ``HandlerInfisical.initialize`` is declared ``async`` but performs no
    awaits, and this function is reached from both sync and async call sites
    (``_configured_secret_store`` is memoized behind ``resolve_api_key`` /
    ``resolve_api_key_async``). Driving it with ``asyncio.run`` on a worker
    thread is correct in both -- the same pattern
    :func:`_resolve_api_key_from_running_loop` already uses in this module.

    This function is the single CONSTRUCTION seam: tests replace it to prove
    the wiring without reaching a real Infisical server.
    """
    from omnibase_core.container import ModelONEXContainer
    from omnibase_infra.handlers.handler_infisical import HandlerInfisical as _Handler

    handler = _Handler(ModelONEXContainer())

    result: Queue[BaseException | None] = Queue(maxsize=1)

    def _runner() -> None:
        try:
            asyncio.run(
                handler.initialize(
                    {
                        "host": config.host,
                        "client_id": config.client_id.get_secret_value(),
                        "client_secret": config.client_secret.get_secret_value(),
                        "project_id": str(config.project_id),
                        "environment_slug": config.environment_slug,
                        "secret_path": config.secret_path,
                    }
                )
            )
        except BaseException as exc:
            result.put(exc)
        else:
            result.put(None)

    thread = Thread(target=_runner, name="omnimarket-infisical-init", daemon=True)
    thread.start()
    thread.join()
    error = result.get()
    if error is not None:
        raise SecretStoreConfigurationError(
            "Lane declares an Infisical-backed secret source but the Infisical "
            f"handler could not be initialized against {config.host!r} "
            f"(environment {config.environment_slug!r}, path "
            f"{config.secret_path!r}): {type(error).__name__}"
        ) from error
    return handler


def _lane_infisical_handler(
    config: ModelSecretResolverConfig,
) -> HandlerInfisical | None:
    """Return the Infisical handler this lane's config requires, or ``None``.

    OMN-16984 AC1/AC2. A lane needs a handler when it DECLARES an Infisical
    source, or when it declares itself controlled via ``INFISICAL_REQUIRED``.
    In either case an unbuildable handler is a fail-fast refusal at store
    construction, naming the offending logical names -- never a per-read
    ``None`` behind a WARNING, which is what made this defect invisible.
    """
    infisical_mappings = [
        mapping
        for mapping in config.mappings
        if mapping.source.source_type == "infisical"
    ]
    required = _infisical_required()
    if not infisical_mappings and not required:
        return None

    folders = sorted(
        {
            _infisical_folder(mapping.source.source_path)
            for mapping in infisical_mappings
        }
    )
    if len(folders) > 1:
        raise SecretStoreConfigurationError(
            "Lane declares Infisical-backed sources in more than one folder, "
            f"which the resolver cannot address: {folders}. SecretResolver "
            "reads every Infisical mapping through the handler's single "
            "configured secret_path, so a second folder would silently read "
            "from the wrong one. Offending logical names: "
            f"{sorted(mapping.logical_name for mapping in infisical_mappings)}."
        )
    # No declared folder means the lane is controlled (INFISICAL_REQUIRED) but
    # routes nothing through Infisical yet; the handler is still built so the
    # credential is proven, and no mapping can reach this path.
    secret_path = folders[0] if folders else "/"

    try:
        handler_config = _infisical_bootstrap_config(secret_path)
    except SecretStoreConfigurationError as exc:
        raise SecretStoreConfigurationError(
            f"{exc} Declared Infisical logical names: "
            f"{sorted(mapping.logical_name for mapping in infisical_mappings)}; "
            f"INFISICAL_REQUIRED={required}."
        ) from exc
    return _build_infisical_handler(handler_config)


def _load_lane_resolver_config(config_path: str) -> ModelSecretResolverConfig:
    """Load the lane's secret-resolver config from its declared sources.

    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` names the artifact
    ``omnibase_infra.runtime.render_secret_resolver_config`` writes at boot,
    but that renderer runs only in ``entrypoint-runtime.sh``. Workloads that
    inherit the variable from a namespace-wide ConfigMap while overriding the
    image command (``onex-api``, ``omnimarket-projection-*``) never render it,
    and this function used to crash there with a bare ``FileNotFoundError``
    from ``Path.read_text``.

    The declared inline ``ONEX_SECRET_RESOLVER_CONFIG_JSON`` is the SAME source
    the renderer itself reads first, so consulting it when the rendered
    artifact is absent is not a fallback default -- it is the same contract
    read from its authoritative form. With neither present this raises a
    typed, attributable error naming both surfaces (CLAUDE.md rule 8).
    """
    path = Path(config_path)
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SecretStoreConfigurationError(
                f"Secret resolver config at {config_path!r} "
                "(ONEX_SECRET_RESOLVER_CONFIG_PATH) could not be read: "
                f"{type(exc).__name__}"
            ) from exc
        origin = f"ONEX_SECRET_RESOLVER_CONFIG_PATH={config_path!r}"
    else:
        inline = os.environ.get(  # ONEX_EXCLUDE: secret_resolver
            "ONEX_SECRET_RESOLVER_CONFIG_JSON", ""
        ).strip()
        if not inline:
            raise SecretStoreConfigurationError(
                "ONEX_SECRET_RESOLVER_CONFIG_PATH declares a lane secret "
                f"mapping at {config_path!r} but no file exists there and "
                "ONEX_SECRET_RESOLVER_CONFIG_JSON is unset. The rendered "
                "artifact is written by "
                "omnibase_infra.runtime.render_secret_resolver_config, which "
                "runs only in entrypoint-runtime.sh -- a workload that "
                "overrides the image command must either render it or declare "
                "the mapping inline."
            )
        try:
            raw = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise SecretStoreConfigurationError(
                "ONEX_SECRET_RESOLVER_CONFIG_JSON is not valid JSON: "
                f"{type(exc).__name__}"
            ) from exc
        origin = "ONEX_SECRET_RESOLVER_CONFIG_JSON"

    if not isinstance(raw, dict):
        raise SecretStoreConfigurationError(
            f"Secret resolver config from {origin} must have a mapping root, "
            f"got {type(raw).__name__}"
        )
    try:
        return ModelSecretResolverConfig.model_validate(raw)
    except ValidationError as exc:
        raise SecretStoreConfigurationError(
            f"Secret resolver config from {origin} failed validation; "
            f"offending fields: {[err.get('loc') for err in exc.errors()]}"
        ) from exc


@lru_cache(maxsize=1)
def _configured_secret_store() -> ProtocolSecretStore | None:
    """Return a lane-configured logical secret store when one is declared."""
    config_path = os.environ.get(  # ONEX_EXCLUDE: secret_resolver
        "ONEX_SECRET_RESOLVER_CONFIG_PATH", ""
    ).strip()
    if not config_path:
        return None

    config = _load_lane_resolver_config(config_path)
    resolver = SecretResolver(
        config=config,
        infisical_handler=_lane_infisical_handler(config),
    )
    store: ProtocolSecretStore = _MappedSecretStore(resolver)
    return store


def clear_secret_store_resolver_cache() -> None:
    """Clear cached lane secret mapping state after config changes in tests."""
    _configured_secret_store.cache_clear()


def _default_secret_store() -> ProtocolSecretStore:
    """Return the default effect-boundary secret store.

    When ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` is set, logical secret refs are
    resolved through the explicit lane secret mapping before reaching the concrete
    source (that path is authoritative and unchanged).

    Otherwise (OMN-13861) the default store is a ``_ConventionFallbackSecretStore``
    (literal env lookup, then dotted-ref → ENV_VAR convention), NOT a bare
    ``AdapterEnvSecretStore``. The routing authority carries dotted logical
    ``secret_ref``s (e.g. ``llm.glm.api_key``); the bare env adapter did a LITERAL
    ``os.environ.get("llm.glm.api_key")`` that never matched the canonical
    ``LLM_*_API_KEY`` env vars, so every authenticated cloud delegation call failed
    to resolve its key on the bus-less local path when no
    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` lane was wired. The composite store adds the
    convention mapping (``llm.glm.api_key`` → ``LLM_GLM_API_KEY``) as a strict
    SUPERSET of the prior literal behavior, so both a literal ref and the canonical
    env-var form resolve. An explicit ``ONEX_SECRET_RESOLVER_CONFIG_PATH``
    (Infisical/lane mapping) still overrides it above.
    """
    configured = _configured_secret_store()
    if configured is not None:
        return configured
    store: ProtocolSecretStore = _ConventionFallbackSecretStore()
    return store


async def resolve_api_key_async(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Resolve the secret VALUE for an ``api_key_ref`` through the secret store.

    Args:
        api_key_ref: The secret reference (key NAME) declared by the routing
            authority, or ``None`` for unauthenticated backends.
        store: The ``ProtocolSecretStore`` to resolve through. Defaults to the
            env-backed store; inject an Infisical-backed store at the deployed
            effect boundary.
        required: When ``True`` (the effect-boundary default), a declared
            reference with no secret-store value fails closed (raises). When
            ``False``, a missing/empty value returns ``None`` — for opt-in
            registration paths where an absent secret simply means "provider
            not configured on this host."
        env_var_fallback: OMN-13943. An additional literal env-var NAME to
            check when the primary ``api_key_ref`` lookup misses. This is
            distinct from the dotted ``secret_ref`` convention (which maps
            through ``SecretResolver``'s ``LLM_*_API_KEY`` naming): it is the
            backend's own contract-declared ``api_key_env`` (e.g.
            ``GEMINI_API_KEY``, ``OPEN_ROUTER_API_KEY``) — the canonical env
            var already defined in ``~/.omnibase/.env``. The caller always
            supplies this from config data, never a hardcoded literal in this
            module, so no provider-specific alias lives in code here.
            **IGNORED for a tenant-minted ref** (OMN-16944): when
            ``api_key_ref`` carries the tenant-credential shape this argument is
            dropped, whatever the call site passed, so a tenant's traffic can
            never be authenticated with a platform (house) key.

    Returns:
        The resolved secret wrapped in ``SecretStr``, or ``None`` when
        ``api_key_ref`` is ``None`` (unauthenticated backend) or when
        ``required`` is ``False`` and the value is absent.

    Raises:
        SecretResolutionError: When ``required`` is ``True`` and ``api_key_ref``
            is declared but neither the secret store nor ``env_var_fallback``
            resolve a non-empty value. Fail-closed; no default. For a
            tenant-minted ref the error is raised by
            :func:`resolve_tenant_scoped_api_key_async` and names the tenant.
    """
    if not api_key_ref:
        return None

    # OMN-16944 AC2/AC3. A ref carrying the minted tenant-credential shape is a
    # TENANT's key. It is routed to the tenant-scoped resolver here, at the one
    # choke point every effect-boundary call site funnels through, so the
    # guarantee holds no matter what an individual call site passes:
    # ``env_var_fallback`` is dropped unconditionally, and a required
    # resolution that misses raises the tenant-attributed error rather than
    # handing back a platform key. Previously this held only because
    # ``ModelInferenceIntent`` happens to carry no ``api_key_env`` -- a property
    # of the current DTO shape, not an enforced one, and
    # ``resolve_tenant_scoped_api_key_async`` had zero production call sites.
    if is_tenant_credential_ref(api_key_ref):
        if not required:
            # Routing-time availability probe. Still no house fallback -- an
            # absent tenant value reports unavailable rather than borrowing one.
            return await _resolve_ref_value(
                api_key_ref,
                store=store,
                required=False,
                env_var_fallback=None,
            )
        return await resolve_tenant_scoped_api_key_async(
            api_key_ref,
            tenant_id=tenant_hint_from_ref(api_key_ref),
            store=store,
        )

    return await _resolve_ref_value(
        api_key_ref,
        store=store,
        required=required,
        env_var_fallback=env_var_fallback,
    )


async def _resolve_ref_value(
    api_key_ref: str,
    *,
    store: ProtocolSecretStore | None,
    required: bool,
    env_var_fallback: str | None,
) -> SecretStr | None:
    """Resolve a declared ref through the store. No tenant/house routing here.

    The single place a secret VALUE is read. :func:`resolve_api_key_async` picks
    which posture to call it with; :func:`resolve_tenant_scoped_api_key_async`
    calls it with the narrowed tenant posture. Keeping the read in one function
    is what makes "a tenant ref never sees ``env_var_fallback``" checkable by
    reading two call sites instead of auditing every boundary.
    """
    resolver = store if store is not None else _default_secret_store()
    value = await resolver.get_secret(api_key_ref)
    if not value and env_var_fallback:
        value = os.environ.get(env_var_fallback) or None
    if not value:
        if not required:
            return None
        fallback_note = (
            f" nor did the declared fallback env var {env_var_fallback!r}"
            if env_var_fallback
            else ""
        )
        raise SecretResolutionError(
            f"Secret reference {api_key_ref!r} declared by the routing authority "
            f"could not be resolved from the secret store (missing or empty){fallback_note}. "
            "No further fallback is permitted."
        )
    return SecretStr(value)


def resolve_api_key(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Synchronous wrapper over :func:`resolve_api_key_async`.

    For use at sync effect boundaries (e.g. ``HandlerInferenceIntent.handle``).
    Drives the async secret store via ``asyncio.run``; raises if called from
    within a running event loop (the async variant must be used there).

    Raises:
        SecretResolutionError: When ``required`` is ``True`` and the declared
            reference cannot be resolved.
        RuntimeError: When invoked from inside a running event loop.
    """
    if not api_key_ref:
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True

    if running_loop:
        raise RuntimeError(
            "resolve_api_key() is sync-only; call resolve_api_key_async() from "
            "an async context."
        )

    return asyncio.run(
        resolve_api_key_async(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )
    )


def _resolve_api_key_from_running_loop(
    api_key_ref: str,
    *,
    store: ProtocolSecretStore | None,
    required: bool,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Resolve from sync code that is already executing inside an event loop."""
    result: Queue[tuple[SecretStr | None, BaseException | None]] = Queue(maxsize=1)

    def _runner() -> None:
        try:
            resolved = asyncio.run(
                resolve_api_key_async(
                    api_key_ref,
                    store=store,
                    required=required,
                    env_var_fallback=env_var_fallback,
                )
            )
        except BaseException as exc:
            result.put((None, exc))
        else:
            result.put((resolved, None))

    thread = Thread(
        target=_runner,
        name="omnimarket-secret-availability",
        daemon=True,
    )
    thread.start()
    thread.join()
    resolved, exc = result.get()
    if exc is not None:
        raise exc
    return resolved


def resolve_api_key_loop_safe(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Resolve a secret VALUE from SYNC code that may run inside an event loop.

    Identical to :func:`resolve_api_key`, except that when it is invoked from
    within a running event loop the resolution is offloaded to a worker thread
    (via :func:`_resolve_api_key_from_running_loop`) instead of raising the
    "sync-only" guard.

    Use this from a SYNC ``handle()`` that the ONEX runtime may dispatch on the
    event loop: ``LocalRuntimeBusAdapter`` calls a sync handler directly inside
    its async ``on_message``, so a bare :func:`resolve_api_key` would raise
    ``RuntimeError("resolve_api_key() is sync-only ...")`` (OMN-13843). Async
    handlers should keep awaiting :func:`resolve_api_key_async` directly; this
    helper exists only for handlers that must stay synchronous (e.g. because a
    sync orchestrator port calls them in-process).

    Fail-closed semantics are unchanged: with ``required=True`` a declared ref
    with no secret-store value (and no resolvable ``env_var_fallback``) raises
    :class:`SecretResolutionError`.
    """
    if not api_key_ref:
        return None
    try:
        return resolve_api_key(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )
    except RuntimeError as exc:
        if "sync-only" not in str(exc):
            raise
        return _resolve_api_key_from_running_loop(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )


async def resolve_tenant_scoped_api_key_async(
    api_key_ref: str | None,
    *,
    tenant_id: str,
    store: ProtocolSecretStore | None = None,
) -> SecretStr | None:
    """Resolve a TENANT-OVERLAY backend's secret VALUE — fail-fast, no house fallback.

    OMN-15631 v1(a). A backend resolved from a tenant's
    ``delegation_routing_tenant_overlay`` row must never silently fall back to
    an OmniNode house key when its own ``secret_ref`` is missing or unresolved
    — that would route a tenant's traffic (and cost) through the platform's
    own provider account without either party's knowledge. This wrapper is a
    thin, INTENTIONALLY-narrower call of :func:`resolve_api_key_async`:
    ``required`` is always ``True`` (fail-fast on a miss) and
    ``env_var_fallback`` is always omitted — the house ``api_key_env``
    convention (``BifrostBackendRef.api_key_env`` / the bifrost contract's
    ``api_key_env`` field) has no tenant-overlay equivalent by construction
    (migration 0001 carries no ``api_key_env`` column), so there is nothing to
    thread here even by accident.

    Args:
        api_key_ref: The tenant-scoped secret reference name, or ``None`` for
            an explicitly unauthenticated tenant backend.
        tenant_id: The tenant this resolution is scoped to — carried for
            structured logging/error attribution only; the actual store
            lookup key is still ``api_key_ref`` (the store is not itself
            tenant-partitioned in v1(a) — see OMN-13236's ``ProtocolSecretStore``
            contract, which this reuses unchanged).
        store: The ``ProtocolSecretStore`` to resolve through. Defaults to the
            same env-backed store :func:`resolve_api_key_async` uses.

    Returns:
        The resolved secret wrapped in ``SecretStr``, or ``None`` only when
        ``api_key_ref`` is ``None`` (an explicitly unauthenticated backend).

    Raises:
        SecretResolutionError: When ``api_key_ref`` is declared but the
            secret store has no value for it. No further fallback exists.
    """
    if not api_key_ref:
        return None
    try:
        # Calls the shared resolution core directly, NOT resolve_api_key_async:
        # that function routes tenant-shaped refs back here (OMN-16944), so
        # going through it would recurse. ``env_var_fallback=None`` is the whole
        # point of this wrapper and is passed explicitly, never defaulted.
        return await _resolve_ref_value(
            api_key_ref,
            store=store,
            required=True,
            env_var_fallback=None,
        )
    except SecretResolutionError as exc:
        raise SecretResolutionError(
            f"Tenant {tenant_id!r} overlay backend declared secret_ref "
            f"{api_key_ref!r} which could not be resolved from the secret "
            "store. Tenant-overlay backends never fall back to a house key — "
            "the tenant must register this ref in the secret store before "
            "this backend is routable."
        ) from exc


def api_key_ref_available(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    env_var_fallback: str | None = None,
) -> bool:
    """Return whether a secret ref resolves to a non-empty value.

    This is for routing-time availability checks that must avoid selecting a
    backend the active runtime secret store cannot use. It returns only a
    boolean; the route decision still carries the secret reference name, never
    the secret value.

    ``env_var_fallback`` (OMN-13943): when supplied, a backend whose dotted
    ``secret_ref`` convention mapping misses but whose own contract-declared
    literal env var IS set is still reported available — the routing tier
    eligibility check must agree with what the effect boundary will actually
    resolve at call time (:func:`resolve_api_key_async`), or a tier could be
    reported unroutable while its backend is actually callable.
    """
    if not api_key_ref:
        return True
    resolved = resolve_api_key_loop_safe(
        api_key_ref,
        store=store,
        required=False,
        env_var_fallback=env_var_fallback,
    )
    return resolved is not None


__all__: list[str] = [
    "SecretResolutionError",
    "SecretStoreConfigurationError",
    "api_key_ref_available",
    "resolve_api_key",
    "resolve_api_key_async",
    "resolve_api_key_loop_safe",
    "resolve_tenant_scoped_api_key_async",
]
