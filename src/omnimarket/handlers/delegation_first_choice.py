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

from omnimarket.inference.local_byok_credential_adapter import (
    local_byok_key_test_failed,
    local_model_tests_path,
    registered_local_byok_providers,
    resolve_local_byok_credential_models,
    resolve_local_byok_credential_plan,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import (
    registered_local_model_routes,
    resolve_pinned_byok_route,
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


def write_first_choice(provider: str, model: str) -> None:
    """Record ``model`` of ``provider`` as the model tried first."""
    path = first_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"provider": provider, "model": model}, indent=2), encoding="utf-8"
    )
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
    if provider == _LOCAL_URL_PROVIDER:
        local = registered_local_model_routes(task_type)
        return next((route for route in local if route.model_id == model), None)
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
    return route.model_copy(update={"model_id": model})


__all__: list[str] = [
    "FIRST_CHOICE_FILE",
    "clear_first_choice",
    "first_choice_path",
    "first_choice_route",
    "read_first_choice",
    "write_first_choice",
]
