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

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers import model_catalog
from omnimarket.handlers.routing.byok_provider_backends import (
    customer_provider_catalogue,
    resolve_byok_provider_backend,
    token_limit_field_for_endpoint,
)

pytestmark = pytest.mark.unit

#: The production pricing read; the suite's autouse fixture empties it per test.
_PRODUCTION_PRICING_INDEX = model_catalog.pricing_models_dev_index

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


@pytest.mark.parametrize(
    ("published", "supplement", "stale", "expected"),
    [
        (131072, 80000, False, 131072),  # provider wins, even above the old cap
        (110000, 90000, False, 110000),  # changed provider value follows
        (None, 90000, False, 90000),  # a dated supplement fills a missing value
        (
            None,
            110000,
            True,
            110000,
        ),  # a session cannot renew a kept copy, so age never refuses it
        (None, None, False, 65536),  # keep the established fallback for reasoning
    ],
)
def test_registered_model_budget_follows_the_known_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: int | None,
    supplement: int | None,
    stale: bool,
    expected: int,
) -> None:
    """Reproduce the actual Reflection response shape and its registered route."""
    from omnimarket.handlers import model_catalog as catalog
    from omnimarket.handlers.routing import byok_model_discovery
    from omnimarket.handlers.routing import local_byok_route as routing
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models
    from omnimarket.handlers.routing.delegation_backend_resolution import (
        resolve_effective_max_tokens,
    )
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _listed_entry

    name = "Beam-1.0-501B-A23B"
    model = {"id": name, "context_length": 1048576, "max_output_tokens": published}

    def unexpected_request(*args: object, **kwargs: object) -> None:
        pytest.fail("resolving a registered route attempted a network read")

    monkeypatch.setattr(catalog, "_onex_home", lambda: tmp_path)
    monkeypatch.setattr(catalog, "_fetch", unexpected_request)
    # The suite empties the pricing index for every test; resolving a route reads
    # the kept copy through the production function.
    monkeypatch.setattr(catalog, "pricing_models_dev_index", _PRODUCTION_PRICING_INDEX)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", unexpected_request)
    listing = list_byok_models(
        "reflection", "unused-fixture", get=lambda **_: {"data": [model]}
    )
    assert listing.outcome == "listed"
    assert listing.models[0].context_tokens == 1048576
    assert listing.models[0].max_output_tokens == published
    catalog.remember_provider_listing(listing)
    index = catalog.EMPTY_INDEX
    if supplement is not None:
        document = {"reflection": {"models": {name: {"limit": {"output": supplement}}}}}
        index = catalog.load_models_dev(
            tmp_path, fetch=lambda *_: json.dumps(document).encode()
        )
        if stale:
            cache = tmp_path / catalog.catalog_config()["models_dev"]["cache_file"]
            os.utime(cache, (0, 0))
    monkeypatch.setattr(
        routing, "resolve_local_byok_credential_ref", lambda *_a, **_k: "fixture-ref"
    )
    monkeypatch.setattr(
        routing, "resolve_local_byok_credential_model", lambda *_a, **_k: name
    )
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    db = tmp_path / "state.sqlite"
    route = routing._registered_byok_route(
        row, replaced=None, tier="cheap_cloud", db_path=db, pinned=True
    )
    assert route is not None
    assert route.max_tokens == expected
    if not stale and (published is not None or supplement is not None):
        assert (
            _listed_entry("reflection", listing.models[0], index).max_output_tokens
            == route.max_tokens
        )
    assert (
        resolve_effective_max_tokens(
            requested=4096, backend_max_tokens=route.max_tokens
        )
        == 4096
    )
    capped = routing._registered_byok_route(
        row.model_copy(update={"max_tokens": 4096}),
        replaced=None,
        tier="cheap_cloud",
        db_path=db,
        pinned=True,
    )
    assert capped is not None
    assert capped.max_tokens == 4096
    assert token_limit_field_for_endpoint(_ENDPOINT) == "max_completion_tokens"
    assert row.timeout_ms >= 300000


def test_retry_budget_uses_the_next_models_own_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers import model_catalog as catalog
    from omnimarket.handlers.routing import local_byok_route as routing
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    db = tmp_path / "state.sqlite"
    ids = ("model-first", "model-second")
    entries = [
        {"id": name, "max_output_tokens": cap}
        for name, cap in zip(ids, (2048, 104000), strict=True)
    ]
    monkeypatch.setattr(catalog, "_onex_home", lambda: tmp_path)
    monkeypatch.setattr(
        catalog, "_fetch", lambda *_: pytest.fail("unexpected network read")
    )
    catalog.remember_provider_listing(
        list_byok_models(
            "reflection", "unused-fixture", get=lambda **_: {"data": entries}
        )
    )
    monkeypatch.setattr(
        routing, "resolve_local_byok_credential_ref", lambda *_a, **_k: "fixture-ref"
    )
    monkeypatch.setattr(
        routing, "resolve_local_byok_credential_model", lambda *_a, **_k: ids[0]
    )
    monkeypatch.setattr(
        routing, "resolve_local_byok_credential_models", lambda *_a, **_k: ids
    )
    monkeypatch.setattr(routing, "local_byok_key_test_failed", lambda *_a, **_k: False)
    row = resolve_byok_provider_backend("reflection")
    assert row is not None
    first = routing._registered_byok_route(
        row, replaced=None, tier="cheap_cloud", db_path=db, pinned=True
    )
    assert first is not None
    second = routing.next_chosen_model_byok_route(
        first, tried_models=frozenset(), db_path=db
    )
    assert second is not None
    assert (first.model_id, first.max_tokens) == (ids[0], 2048)
    assert (second.model_id, second.max_tokens) == (ids[1], 104000)


def test_packaged_reflection_policy_has_no_literal_output_ceiling() -> None:
    config = yaml.safe_load(
        (_CATALOG.parent / "byok_provider_backends.v1.yaml").read_text()
    )

    def literal_limits(rows: list[dict]) -> list[int]:
        return [
            r["max_tokens"]
            for r in rows
            if r["provider"] == "reflection" and r.get("max_tokens") is not None
        ]

    rows = config["providers"]
    assert (
        literal_limits([*rows, {"provider": "reflection", "max_tokens": 70961}])[-1]
        == 70961
    )
    assert literal_limits(rows) == []


@pytest.mark.parametrize("stated", [True, 1.9, "90000", 90000])
def test_invalid_provider_limit_preserves_the_next_valid_source(stated: object) -> None:
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    model = {
        "id": "beam-fixture",
        "max_output_tokens": stated,
        "top_provider": {"max_completion_tokens": 81000},
    }
    listing = list_byok_models(
        "reflection", "fixture", get=lambda **_: {"data": [model]}
    )
    assert listing.models[0].max_output_tokens == (
        81000 if stated is True or stated == 1.9 else 90000
    )


@pytest.mark.parametrize(
    ("requested", "supplement", "expected"),
    [
        (100000, 131072, 100000),
        (4096, 131072, 4096),
        (100000, None, 65536),
    ],
)
def test_first_dispatch_and_receipt_use_the_kept_output_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: int,
    supplement: int | None,
    expected: int,
) -> None:
    from pydantic import SecretStr

    from omnimarket.handlers import model_catalog as catalog
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_llm_delegation_call as call,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
    from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
        HandlerLocalSecretStore,
    )
    from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
        ModelLocalSecretRequest,
    )
    from tests.chains.local.harness import use_local_store
    from tests.helpers.tenant_registry import mirror_tenant_in_store

    name = "Beam-1.0-501B-A23B"
    reads = []
    posted = []

    def read_catalog(*args: object) -> bytes:
        reads.append("models.dev")
        if supplement is None:
            raise OSError("supplement unavailable")
        return json.dumps(
            {"reflection": {"models": {name: {"limit": {"output": supplement}}}}}
        ).encode()

    def post(*, payload, **kwargs):
        posted.append(payload["max_completion_tokens"])
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": "### ANSWER\nA hash map finds a stored value by hashing its key into the matching bucket."
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 19},
            },
            latency_ms=2,
        )

    monkeypatch.setattr(catalog, "_onex_home", lambda: tmp_path)
    monkeypatch.setattr(catalog, "_fetch", read_catalog)
    # The suite empties the pricing index for every test; the call reads the kept
    # copy through the production function.
    monkeypatch.setattr(catalog, "pricing_models_dev_index", _PRODUCTION_PRICING_INDEX)
    # Setup is where models.dev is read: the one read, before any call.
    catalog.load_models_dev(tmp_path, force=True)
    setup_reads = list(reads)
    monkeypatch.setattr(call, "_is_endpoint_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(transport, "get_served_model_ids", lambda *_a, **_k: None)
    monkeypatch.setattr(transport, "post_chat_completion", post)
    db = use_local_store(monkeypatch, tmp_path)
    mirror_tenant_in_store(db, "acme")
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref="llm.reflection.api_key",
            value=SecretStr("fixture"),
            models=(name,),
            force=True,
        )
    )
    port = LocalDelegationDispatchPort(
        effect_handler=call.HandlerLlmDelegationCall(),
        evidence_db_path=db,
        effect_process_boundary=False,
    )
    dispatch = port.dispatch(
        prompt="Explain how a hash map finds a value.",
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=requested,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=90,
        terminal_delivery_margin_seconds=10,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="acme",
        backend_id="byok-reflection",
    )
    result = asyncio.run(dispatch)
    assert result["status"] == "completed", result
    assert len(result["attempts"]) == 1
    assert reads == setup_reads, "the call read models.dev"
    assert posted == [expected]
    assert result["attempts"][0]["configuration"]["max_tokens"] == expected


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
