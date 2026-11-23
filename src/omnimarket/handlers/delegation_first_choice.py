# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model a developer chose for delegation to try first (OMN-21346).

``onex models first PROVIDER [MODEL]`` records one ``(provider, model)`` beside
the local store. A run on this machine tries its route before the local models
and before the ladder; when it fails, the run carries on in the order it would
have taken without a choice. A choice that no longer resolves (the key or the
local model was removed, the model left the list, or the key failed its last
test) is ignored, so the run keeps its usual order.

Nothing here reads a key: the route carries the customer's minted reference,
resolved at the effect boundary like every other route.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from omnimarket.handlers.routing.byok_provider_backends import (
    resolve_byok_provider_backend,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
)
from omnimarket.handlers.routing.local_byok_route import (
    registered_local_model_routes,
    resolve_pinned_byok_route,
    with_byok_model,
)
from omnimarket.inference.local_byok_credential_adapter import (
    local_byok_key_test_failed,
    local_model_tests_path,
    registered_local_byok_providers,
    resolve_local_byok_credential_models,
    resolve_local_byok_credential_plan,
)
from omnimarket.nodes.node_model_setup_effect.connection_storage import (
    read_named_connections,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
    ModelEndpointConnection,
)

FIRST_CHOICE_FILE: Final = "delegation_first_choice.json"

#: The provider name ``onex models`` gives the local models declared by URL.
_LOCAL_URL_PROVIDER: Final = "local"


def first_choice_path(db_path: Path | None = None) -> Path:
    """Where ``onex models first`` records the model delegation tries first: beside the tests."""
    return local_model_tests_path(db_path).with_name(FIRST_CHOICE_FILE)


def read_first_choice(*, db_path: Path | None = None) -> tuple[str, str] | None:
    """The ``(provider, model)`` chosen to be tried first, or ``None``.

    ``None`` when nothing was chosen or the record cannot be read. Whether the
    choice still resolves is the caller's question.
    """
    try:
        recorded = json.loads(first_choice_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(recorded, dict):
        return None
    provider, model = recorded.get("provider"), recorded.get("model")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        return provider, model
    return None


def read_first_choice_url(*, db_path: Path | None = None) -> str | None:
    """The server URL recorded with a local first choice (OMN-23413), or ``None``.

    Two local servers can run the same model; the URL says which one was chosen.
    """
    try:
        recorded = json.loads(first_choice_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = recorded.get("url") if isinstance(recorded, dict) else None
    return url if isinstance(url, str) and url else None


def write_first_choice(provider: str, model: str, url: str | None = None) -> None:
    """Record ``model`` of ``provider`` as the model tried first, on ``url`` when given."""
    path = first_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    record = {"provider": provider, "model": model}
    if url:
        record["url"] = url
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_first_choice() -> None:
    """Forget the choice; runs keep their usual order."""
    first_choice_path().unlink(missing_ok=True)


def first_choice_route(
    task_type: str, *, db_path: Path | None = None
) -> ModelResolvedDelegationBackend | None:
    """The route of the model chosen to be tried first, or ``None`` when it no longer resolves."""
    choice = read_first_choice(db_path=db_path)
    if choice is None:
        return None
    provider, model = choice
    if provider.startswith("connection:"):
        name = provider.removeprefix("connection:")
        found = next(
            (
                row
                for row in read_named_connections(db_path=db_path)
                if row["connection_id"] == name
            ),
            None,
        )
        if found is None:
            return None
        connection = ModelEndpointConnection.model_validate(found)
        if model not in connection.models:
            return None
        try:
            named_route = resolve_delegation_backend(
                task_type,
                backend_id=connection.backend_id(model),
                overlay_path=local_model_tests_path(db_path).with_name(
                    "bifrost_overrides.yaml"
                ),
            )
        except (OSError, ValueError):
            return None
        if (
            named_route.model_id != model
            or named_route.endpoint_ref != connection.url + "/v1/chat/completions"
            or named_route.secret_ref != connection.secret_ref
        ):
            return None
        return named_route
    if provider == _LOCAL_URL_PROVIDER:
        # OMN-23413: with the server recorded, only that server's route is the
        # choice; another server running the same model is not.
        url = read_first_choice_url(db_path=db_path)
        endpoint = f"{url}/v1/chat/completions" if url else None
        local = registered_local_model_routes(task_type)
        return next(
            (
                route
                for route in local
                if route.model_id == model
                and (endpoint is None or route.endpoint_ref == endpoint)
            ),
            None,
        )
    if provider not in registered_local_byok_providers(db_path=db_path):
        return None
    if model not in resolve_local_byok_credential_models(provider, db_path=db_path):
        return None
    plan = resolve_local_byok_credential_plan(provider, db_path=db_path)
    byok = resolve_byok_provider_backend(provider, plan=plan)
    if byok is None or not byok.customer_routable:
        return None
    # A key that failed its last test is not routed (OMN-21259); the pinned
    # resolution below does not check that, since a pinned test re-proves it.
    if local_byok_key_test_failed(provider, db_path=db_path):
        return None
    route = resolve_pinned_byok_route(byok.backend_id, db_path=db_path)
    if route is None:
        return None
    return with_byok_model(route, model)


def first_choice_routes(
    task_type: str, *, db_path: Path | None = None
) -> tuple[ModelResolvedDelegationBackend, ...]:
    """Selected named model followed by that connection's remaining ordered models.

    Each route must still match its canonical endpoint, model and credential.
    Legacy selections remain one route; pins never call this enumeration.
    """
    first = first_choice_route(task_type, db_path=db_path)
    if first is None:
        return ()
    choice = read_first_choice(db_path=db_path)
    if choice is None or not choice[0].startswith("connection:"):
        return (first,)
    row = next(
        (
            item
            for item in read_named_connections(db_path=db_path)
            if item["connection_id"] == choice[0].removeprefix("connection:")
        ),
        None,
    )
    if row is None:
        return ()
    connection = ModelEndpointConnection.model_validate(row)
    routes = [first]
    for model in connection.models:
        if model == first.model_id:
            continue
        route = resolve_delegation_backend(
            task_type,
            backend_id=connection.backend_id(model),
            overlay_path=local_model_tests_path(db_path).with_name(
                "bifrost_overrides.yaml"
            ),
        )
        if (
            route.model_id != model
            or route.endpoint_ref != first.endpoint_ref
            or route.secret_ref != connection.secret_ref
        ):
            raise ValueError(
                "A named connection route no longer matches its registration"
            )
        routes.append(route)
    return tuple(routes)


def selected_model_routes(
    task_type: str, *, db_path: Path | None = None
) -> tuple[ModelResolvedDelegationBackend, ...]:
    """Every model the developer selected in onex, in the order delegation tries them.

    OMN-23413 AC22: what ``onex models`` and the TUI's /config stored, and
    nothing else: the model chosen first, then the Local Models declared by
    URL, then each key's chosen models (keys in registration order, each key's
    models in its own order), then each named
    connection's models. A key whose last test failed is left out (OMN-21259).
    The same order ``onex models list --ladder`` shows, with named connections
    after it. Rungs another tool wrote into the overlay (the built-in local
    tier) are not a selection and are never part of it.
    """
    routes: list[ModelResolvedDelegationBackend] = [
        *first_choice_routes(task_type, db_path=db_path),
        *registered_local_model_routes(task_type),
    ]
    # Keys in the order they were registered: the order dispatch walks them.
    for provider in registered_local_byok_providers(
        db_path=db_path, registration_order=True
    ):
        if provider == _LOCAL_URL_PROVIDER:
            continue
        if local_byok_key_test_failed(provider, db_path=db_path):
            continue
        plan = resolve_local_byok_credential_plan(provider, db_path=db_path)
        byok = resolve_byok_provider_backend(provider, plan=plan)
        if byok is None or not byok.customer_routable:
            continue
        route = resolve_pinned_byok_route(byok.backend_id, db_path=db_path)
        if route is None:
            continue
        models = resolve_local_byok_credential_models(provider, db_path=db_path)
        if not models:
            routes.append(route)
        for chosen in models:
            routes.append(with_byok_model(route, chosen))
    overlay = local_model_tests_path(db_path).with_name("bifrost_overrides.yaml")
    for row in read_named_connections(db_path=db_path):
        connection = ModelEndpointConnection.model_validate(row)
        for model in connection.models:
            try:
                named = resolve_delegation_backend(
                    task_type,
                    backend_id=connection.backend_id(model),
                    overlay_path=overlay,
                )
            except (OSError, ValueError):
                continue
            if (
                named.model_id == model
                and named.endpoint_ref == connection.url + "/v1/chat/completions"
                and named.secret_ref == connection.secret_ref
            ):
                routes.append(named)
    seen: set[tuple[str, str]] = set()
    ordered: list[ModelResolvedDelegationBackend] = []
    for route in routes:
        key = (route.backend_id, route.model_id)
        if key not in seen:
            seen.add(key)
            ordered.append(route)
    return tuple(ordered)


__all__: list[str] = [
    "FIRST_CHOICE_FILE",
    "clear_first_choice",
    "first_choice_path",
    "first_choice_route",
    "first_choice_routes",
    "read_first_choice",
    "read_first_choice_url",
    "selected_model_routes",
    "write_first_choice",
]
