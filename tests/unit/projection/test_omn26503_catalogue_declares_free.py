# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26503: ``_catalogue_declares_free`` prices a model with the catalogue's current call.

omnimarket#6594 replaced ``byok_model_price_per_1m``'s ``supplemental`` argument
with ``models_dev_price`` (default ``None``, which stops before models.dev), but
``_catalogue_declares_free`` still passed ``supplemental=False`` and raised
``TypeError``. The reading it keeps is catalogue-only: the declared catalogue
price, then the model registry's, never models.dev.

Cases, each over the shipped z.ai general-API row:
  F1  a model the catalogue declares at price zero is free
  F2  a model the catalogue declares at a nonzero price is not free
  F3  a model neither the catalogue nor the registry prices is not free
  F4  no models.dev lookup is passed to the price call or made
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from omnimarket.handlers import model_catalog
from omnimarket.handlers.routing import byok_provider_backends as catalogue
from omnimarket.models.priced_spend import _catalogue_declares_free

pytestmark = pytest.mark.unit

_BACKEND = "byok-glm-general"
_DECLARED_FREE = "glm-4.5-flash"
_DECLARED_PAID = "glm-5.3-flash"
_UNPRICED = "no-such-model-omn26503"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    _catalogue_declares_free.cache_clear()
    yield
    _catalogue_declares_free.cache_clear()


@pytest.fixture
def models_dev_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record any models.dev lookup, and make the price call's keyword visible."""
    calls: list[tuple[str, str]] = []

    def _forbidden(provider: str, model: str, *args: Any, **kwargs: Any) -> None:
        calls.append((provider, model))
        raise AssertionError("models.dev must not be read for a free-price check")

    monkeypatch.setattr(model_catalog, "models_dev_price_with_read_time", _forbidden)
    return calls


def test_catalogue_declares_free_zero_priced_model_is_free(
    models_dev_calls: list[tuple[str, str]],
) -> None:
    """F1: the catalogue's declared zero price reads as free."""
    assert _catalogue_declares_free(_BACKEND, _DECLARED_FREE) is True
    assert models_dev_calls == []


def test_catalogue_declares_free_nonzero_priced_model_is_not_free(
    models_dev_calls: list[tuple[str, str]],
) -> None:
    """F2: a declared nonzero price is not free."""
    assert _catalogue_declares_free(_BACKEND, _DECLARED_PAID) is False
    assert models_dev_calls == []


def test_catalogue_declares_free_unpriced_model_is_not_free(
    models_dev_calls: list[tuple[str, str]],
) -> None:
    """F3: no price from the catalogue or the registry is not free."""
    assert _catalogue_declares_free(_BACKEND, _UNPRICED) is False
    assert models_dev_calls == []


def test_catalogue_declares_free_unknown_backend_is_not_free() -> None:
    """A backend the catalogue does not declare has no row to read: not free."""
    assert _catalogue_declares_free("byok-not-a-backend", _DECLARED_FREE) is False


def test_catalogue_declares_free_passes_no_models_dev_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4: the price call carries no models.dev lookup, so it stops before it."""
    real = catalogue.byok_model_price_per_1m
    seen: list[dict[str, Any]] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(catalogue, "byok_model_price_per_1m", _spy)
    assert _catalogue_declares_free(_BACKEND, _DECLARED_FREE) is True
    assert seen, "the price call was not made"
    assert all(kwargs.get("models_dev_price") is None for kwargs in seen)
