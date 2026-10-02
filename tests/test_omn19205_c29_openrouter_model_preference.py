# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OpenRouter catalogue row prefers a Google-hosted model over the Nvidia one.

C29 (a customer's own provider key delegates locally) graded red on three
scheduled runs because the nemotron-ultra free slug answered HTTP 200 carrying
"Upstream error from Nvidia: Service temporarily overloaded" (code 503).
"""

from __future__ import annotations

from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    resolve_byok_provider_backend,
    select_byok_model,
)

NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free"
GEMMA = "google/gemma-4-31b-it:free"
GEMMA_MOE = "google/gemma-4-26b-a4b-it:free"


def _row() -> ModelByokProviderBackend:
    row = resolve_byok_provider_backend("openrouter")
    assert row is not None
    return row


def test_a_key_listing_both_families_runs_the_google_hosted_model() -> None:
    listed = [NEMOTRON, GEMMA, GEMMA_MOE]
    assert select_byok_model(_row(), listed) == GEMMA
    assert select_byok_model(_row(), list(reversed(listed))) == GEMMA


def test_the_nvidia_model_remains_the_fallback_when_no_google_model_is_listed() -> None:
    assert select_byok_model(_row(), [NEMOTRON, "other/pro"]) == NEMOTRON
    assert select_byok_model(_row(), [NEMOTRON, GEMMA], exclude=(GEMMA,)) == NEMOTRON


def test_the_google_family_pattern_rejects_paid_and_other_slugs() -> None:
    row = _row()
    assert select_byok_model(row, ["google/gemma-4-31b-it"]) is None
    assert select_byok_model(row, ["google/gemini-3.5-flash:free"]) is None
