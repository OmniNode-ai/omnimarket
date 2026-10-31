# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The developer's rule limiting delegation to local or free models (OMN-21267).

``onex models only local`` (or ``free``) records one rule beside the local
store; ``onex models only --clear`` removes it. With a rule set, a delegation on
this machine skips every model outside it before any attempt and names the
skip; when the rule leaves no model, the run is refused before any provider
call. With no rule, routing is what it was.

A local model is one the developer runs: a model ``onex models add local``
declared by URL, or a keyless rung of the local tier (Ollama). A free model is
a local one, or one the provider catalogue declares free: a plan billed as a
free tier, a model the catalogue tags ``free`` (an OpenRouter ``:free`` id), or
a model whose declared price is zero. Anything else counts as paid.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, cast, get_args

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.local_byok_credential_adapter import local_model_tests_path
from omnimarket.routing.byok_provider_backends import (
    byok_declared_price_per_1m,
    resolve_byok_backend_by_id,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import is_local_url_backend

ModelScopeRule = Literal["local", "free"]
MODEL_SCOPE_RULES: Final[tuple[str, ...]] = get_args(ModelScopeRule)
MODEL_SCOPE_FILE: Final = "delegation_model_scope.json"
#: The code a run refused by the rule carries.
MODEL_SCOPE_REFUSAL_CODE: Final = "MODEL_SCOPE_LEAVES_NO_MODEL"
_RULE_TEXT: Final[dict[str, str]] = {
    "local": "local models only",
    "free": "free models only",
}


class DelegationModelScopeRefusedError(ProtocolConfigurationError):
    """The developer's rule left no model to try; no provider was called."""


def model_scope_path(db_path: Path | None = None) -> Path:
    """Where ``onex models only`` records the rule: beside the model tests."""
    return local_model_tests_path(db_path).with_name(MODEL_SCOPE_FILE)


def read_model_scope(*, db_path: Path | None = None) -> ModelScopeRule | None:
    """The rule in force, or ``None`` when none is set or the record is unreadable."""
    try:
        recorded = json.loads(model_scope_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rule = recorded.get("only") if isinstance(recorded, dict) else None
    return cast(ModelScopeRule, rule) if rule in MODEL_SCOPE_RULES else None


def write_model_scope(rule: ModelScopeRule) -> None:
    """Record ``rule``; it replaces any rule already set."""
    if rule not in MODEL_SCOPE_RULES:
        raise ValueError(f"unknown model rule {rule!r}; one of {MODEL_SCOPE_RULES}")
    path = model_scope_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"only": rule}, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_model_scope() -> None:
    """Remove the rule; delegation routes as it did without one."""
    model_scope_path().unlink(missing_ok=True)


def model_scope_text(rule: ModelScopeRule) -> str:
    """The rule as ``onex models list`` and a skip reason say it."""
    return _RULE_TEXT[rule]


def is_local_model(
    backend: ModelResolvedDelegationBackend, *, is_local_rung: Callable[[str], bool]
) -> bool:
    """A model the developer runs: declared by URL, or a keyless local-tier rung."""
    if is_local_url_backend(backend.backend_id):
        return True
    keyless = backend.secret_ref is None and backend.api_key_env is None
    return keyless and is_local_rung(backend.backend_id)


def is_free_model(
    backend: ModelResolvedDelegationBackend, *, is_local_rung: Callable[[str], bool]
) -> bool:
    """A local model, or one the provider catalogue declares free."""
    if is_local_model(backend, is_local_rung=is_local_rung):
        return True
    row = resolve_byok_backend_by_id(backend.backend_id)
    if row is None:
        return False
    if row.limit_model.billing == "free_tier":
        return True
    if any(
        "free" in preference.tags
        and preference.generation_of(backend.model_id) is not None
        for preference in row.model_preference
    ):
        return True
    price = byok_declared_price_per_1m(row.endpoint_url, backend.model_id)
    return price is not None and price == (Decimal(0), Decimal(0))


def model_scope_skip_reason(
    backend: ModelResolvedDelegationBackend,
    rule: ModelScopeRule | None,
    *,
    is_local_rung: Callable[[str], bool],
) -> str | None:
    """Why ``backend`` is skipped under ``rule``, or ``None`` when it may be tried."""
    if rule is None:
        return None
    allowed = (
        is_local_model(backend, is_local_rung=is_local_rung)
        if rule == "local"
        else is_free_model(backend, is_local_rung=is_local_rung)
    )
    if allowed:
        return None
    kind = "not a local model" if rule == "local" else "not a free model"
    return (
        f"model skipped before dispatch: {backend.model_id} on "
        f"{backend.backend_id} is {kind}, and your rule is "
        f"{model_scope_text(rule)} (onex models only --clear removes it)"
    )


def model_scope_refusal(
    rule: ModelScopeRule, skips: list[dict[str, object]]
) -> DelegationModelScopeRefusedError:
    """The named refusal for a run whose rule left no model to try."""
    skipped = ", ".join(f"{s['model_id']} on {s['backend_id']}" for s in skips)
    return DelegationModelScopeRefusedError(
        f"{MODEL_SCOPE_REFUSAL_CODE}: your rule is {model_scope_text(rule)}, and "
        f"no model you set up satisfies it (skipped: {skipped or 'none'}). Add one "
        "that does (onex models add local --url <base url> --model <name>), or "
        "remove the rule with: onex models only --clear"
    )


__all__: list[str] = [
    "MODEL_SCOPE_FILE",
    "MODEL_SCOPE_REFUSAL_CODE",
    "MODEL_SCOPE_RULES",
    "DelegationModelScopeRefusedError",
    "ModelScopeRule",
    "clear_model_scope",
    "is_free_model",
    "is_local_model",
    "model_scope_path",
    "model_scope_refusal",
    "model_scope_skip_reason",
    "model_scope_text",
    "read_model_scope",
    "write_model_scope",
]
