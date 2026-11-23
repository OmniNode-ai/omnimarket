# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23413: the catalogue free-price check calls the price lookup it has.

``byok_model_price_per_1m`` lost its ``supplemental`` keyword when the lookup began taking
``models_dev_price`` (it stops before models.dev when none is passed). ``priced_spend`` still passed
``supplemental=False``, so the strict type check failed on dev and every call into the check raised
``TypeError``.

Failure modes, each with a test below:
  C1  a model on a catalogue backend raises TypeError instead of answering (fails on the pre-fix code)
  C2  over-correction: a declared zero price stops reading as free
  C3  over-correction: a priced model reads as free
  C4  the lookup is called without a models.dev lookup, so the shipped list never prices a call
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from omnimarket.handlers.routing import byok_provider_backends
from omnimarket.models import priced_spend


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    priced_spend._catalogue_declares_free.cache_clear()
    yield
    priced_spend._catalogue_declares_free.cache_clear()


def _a_catalogue_backend() -> str:
    rows = byok_provider_backends.load_byok_plan_catalog().values()
    assert rows, "positive control: the plan catalogue declares at least one backend"
    return next(iter(rows)).backend_id


def test_c1_unlisted_model_on_a_catalogue_backend_answers_not_free() -> None:
    backend_id = _a_catalogue_backend()
    assert (
        priced_spend._catalogue_declares_free(backend_id, "no-such-model-omn23413")
        is False
    )


def test_c2_declared_zero_price_reads_as_free(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def lookup(
        backend_id: str | None,
        endpoint_url: str | None,
        model_name: str,
        *,
        models_dev_price: Any = None,
    ) -> tuple[Decimal, Decimal] | None:
        calls.append({"models_dev_price": models_dev_price})
        return (Decimal(0), Decimal(0))

    monkeypatch.setattr(byok_provider_backends, "byok_model_price_per_1m", lookup)
    assert priced_spend._catalogue_declares_free(_a_catalogue_backend(), "m") is True
    # C4: no models.dev lookup is handed over.
    assert calls
    assert all(c["models_dev_price"] is None for c in calls)


def test_c3_priced_model_does_not_read_as_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(
        backend_id: str | None,
        endpoint_url: str | None,
        model_name: str,
        *,
        models_dev_price: Any = None,
    ) -> tuple[Decimal, Decimal] | None:
        return (Decimal("0.5"), Decimal(2))

    monkeypatch.setattr(byok_provider_backends, "byok_model_price_per_1m", lookup)
    assert priced_spend._catalogue_declares_free(_a_catalogue_backend(), "m") is False
