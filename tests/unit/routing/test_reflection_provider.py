# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reflection AI is offered as a key provider (OMN-23413, operator 2026-11-13).

Facts, each read 2026-11-13 from Reflection's own pages and checked against
models.dev's catalogue (provider id `reflection`):
  - OpenAI-compatible base https://api.reflection.ai/openai/v1, Bearer key
    (developers.reflection.ai/openai-compatibility.md, authentication.md); an
    unauthenticated GET /models answered 401 missing_credentials.
  - One model, Beam-1.0-501B-A23B: 1,048,576-token context, 131,072 output
    (developers.reflection.ai/models.md).
  - max_tokens is deprecated in favour of max_completion_tokens, and both count
    reasoning tokens; reasoning is mandatory, default effort max
    (api-reference/chat/create-a-chat-completion.md, reasoning.md).
  - No published key prefix, rate limit numbers or per-token price.

Failure modes:
  P1  `onex models add reflection` is refused as an unknown provider.
  P2  the row posts to the wrong URL or lists a model Reflection does not serve.
  P3  the deprecated max_tokens is sent, or an output budget too small for
      mandatory max-effort reasoning, so answers are cut off.
  P4  /config does not offer Reflection, or Local stops being the last choice.
  P5  /config's table shows no capabilities or limits for its models (no
      models.dev id), or invents a key prefix Reflection does not publish.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.routing.byok_provider_backends import (
    customer_provider_catalogue,
    resolve_byok_provider_backend,
    token_limit_field_for_endpoint,
)

pytestmark = pytest.mark.unit

_SETUP = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_onex_setup_effect"
)
_CATALOG = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/configs/model_catalog.yaml"
)
_ENDPOINT = "https://api.reflection.ai/openai/v1/chat/completions"


def test_reflection_is_in_the_customer_catalogue() -> None:
    """P1."""
    assert "reflection" in customer_provider_catalogue()


def test_onex_models_add_accepts_reflection() -> None:
    """P1: the installed `onex`, whose provider choice is built from the catalogue."""
    onex = Path(sys.executable).parent / "onex"
    shown = subprocess.run(
        [str(onex), "models", "add", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert shown.returncode == 0, shown.stderr
    # Click wraps the choice list mid-word, so read it with all spaces removed.
    choice = re.search(r"\{([a-z_|]+)\}", "".join(shown.stdout.split()))
    assert choice is not None, shown.stdout
    assert "reflection" in choice.group(1).split("|")


def test_the_row_posts_to_reflections_openai_endpoint() -> None:
    """P2."""
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    assert row.endpoint_url == _ENDPOINT
    assert row.models_url == "https://api.reflection.ai/openai/v1/models"
    assert list(row.default_models) == ["Beam-1.0-501B-A23B"]
    assert row.mirrors_house_rung is False  # the platform holds no Reflection key


def test_the_default_model_is_ranked_by_its_own_pattern() -> None:
    """P2: a default no preference pattern matches would never be picked first."""
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    assert any(
        re.fullmatch(p.pattern, m)
        for p in row.model_preference
        for m in row.default_models
    )


def test_the_token_budget_fits_mandatory_reasoning() -> None:
    """P3."""
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    assert token_limit_field_for_endpoint(_ENDPOINT) == "max_completion_tokens"
    assert 32768 < row.max_tokens <= 131072  # above Reflection's own default
    assert row.timeout_ms >= 300000


def test_config_offers_reflection_before_local() -> None:
    """P4."""
    traits = yaml.safe_load((_SETUP / "setup_provider_traits.yaml").read_text())
    order = [entry["provider"] for entry in traits["picker"]]
    assert "reflection" in order
    assert order[-1] == "local"


def test_the_table_reads_reflection_from_models_dev_and_no_key_prefix() -> None:
    """P5."""
    catalog = yaml.safe_load(_CATALOG.read_text())
    assert catalog["provider_ids"]["reflection"] == "reflection"
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    assert not row.key_prefixes
