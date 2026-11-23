# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26449: the catalogue free-price lookup calls the price reader as it is defined.

``priced_spend._catalogue_declares_free`` asked ``byok_model_price_per_1m`` for
``supplemental=False``, a keyword the reader dropped when it began to stop before
models.dev on its own. mypy refused the call on dev and a row naming a catalogue
backend raised ``TypeError`` instead of reading as priced or unpriced.
"""

from __future__ import annotations

from omnimarket.handlers.routing.byok_provider_backends import load_byok_plan_catalog
from omnimarket.models.priced_spend import _catalogue_declares_free


def test_a_catalogue_backend_with_an_unlisted_model_is_not_declared_free() -> None:
    backend_id = next(iter(load_byok_plan_catalog().values())).backend_id
    _catalogue_declares_free.cache_clear()

    assert _catalogue_declares_free(backend_id, "no-such-model-omn26449") is False
