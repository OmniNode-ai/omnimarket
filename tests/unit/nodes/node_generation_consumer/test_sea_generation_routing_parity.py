# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SEA generation-pipeline ROUTE parity (WS-C Phase 1.2).

The self-extending-agent (SEA) hackathon repo carried a bespoke imperative
generation loop split across three modules:

  * ``src/pipeline/consumer.py``        - the main ``GenerationConsumer`` loop
  * ``src/pipeline_local/consumer_local.py`` - local-tier subclass (Track B)
  * ``src/pipeline/kafka_runner.py``    - cloud-tier Kafka daemon (Track A)

That capability has a permanent canonical home in OmniMarket's
``node_generation_consumer``. This module pins the ROUTE parity boundary so the
canonical node demonstrably covers every capability of the SEA copies, with
endpoints resolved from the routing authority (the bifrost delegation contract
overlay keyed by the contract ``endpoint_ref``) and NOT from environment
variables.

These assertions are deliberately structural and contract-level so they remain
green without a live broker or LLM. The live runtime parity (the canonical node
processing real ``node-generation-requested`` commands on both the local and
cloud tiers) is proven by the deployed consumer group, and the per-tier endpoint
resolution from the contract is proven exhaustively in
``test_endpoint_routing_authority.py``.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_generation_consumer.handlers import (
    handler_generation_consumer as handler_mod,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
    resolve_generation_endpoint,
)

_HANDLER_SRC = Path(handler_mod.__file__).read_text()


def _repo_root() -> Path:
    # handler file: <root>/src/omnimarket/nodes/node_generation_consumer/handlers/...
    here = Path(handler_mod.__file__).resolve()
    for parent in here.parents:
        if (parent / "docs" / "migrations").is_dir():
            return parent
    raise AssertionError("could not locate repo root with docs/migrations")


# ---------------------------------------------------------------------------
# Capability parity: the canonical handler subsumes the SEA GenerationConsumer.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_node_is_the_generation_entrypoint() -> None:
    """The canonical node exposes the single bus-native generation entrypoint.

    SEA's ``GenerationConsumer.run_once`` / ``generate_with_retry`` are replaced
    by ``HandlerGenerationConsumer.handle`` - one async handler over the event
    envelope, dispatched by the runtime, not a hand-rolled consumer loop.
    """
    assert hasattr(HandlerGenerationConsumer, "handle")
    assert inspect.iscoroutinefunction(HandlerGenerationConsumer.handle)


@pytest.mark.unit
def test_canonical_handler_covers_retry_loop_capability() -> None:
    """Parity: bounded retry-on-validation-failure loop is in the canonical node.

    SEA's ``generate_with_retry`` looped ``range(1, max_attempts + 1)`` and
    retried on ``not validation["valid"]``. The canonical ``handle`` carries the
    same bounded loop, driven by the contract-declared ``max_attempts``.
    """
    src = inspect.getsource(HandlerGenerationConsumer.handle)
    assert "for attempt_num in range(1, command.max_attempts + 1)" in src


@pytest.mark.unit
def test_canonical_handler_covers_cost_calculation_capability() -> None:
    """Parity: per-run inference cost is computed in the canonical node.

    SEA's ``_calculate_cost`` / ``_calculate_cost_with_basis`` priced measured
    tokens; the canonical node sources pricing from the cost-pricing contract
    (no hardcoded source constant) via ``_calculate_cost``.
    """
    assert hasattr(handler_mod, "_calculate_cost")
    cost_src = inspect.getsource(handler_mod._calculate_cost)
    assert "lookup_cost_pricing" in cost_src
    assert "load_cost_pricing" in cost_src


@pytest.mark.unit
def test_canonical_handler_aggregates_usage_provenance() -> None:
    """Parity: usage-source provenance aggregation (MEASURED vs UNKNOWN).

    SEA's ``_aggregate_usage_source`` returned MEASURED only when every attempt
    was MEASURED. The canonical node keeps the same provenance discipline (no
    silent ESTIMATED downgrade).
    """
    assert hasattr(handler_mod, "_aggregate_usage_source")


# ---------------------------------------------------------------------------
# Endpoint authority: both tiers resolve from the contract, never from env.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_endpoint_resolution_is_via_routing_authority_not_env() -> None:
    """Both tiers resolve their endpoint from the routing authority.

    SEA resolved endpoints imperatively from the model registry
    (``entry.endpoint_url``) seeded by ``KAFKA_BOOTSTRAP_SERVERS`` /
    ``LLM_*`` env. The canonical node resolves per-model from the bifrost
    delegation contract overlay keyed by ``endpoint_ref`` via
    ``resolve_generation_endpoint`` - provider-agnostic (local + cloud) and
    fail-closed.
    """
    assert callable(resolve_generation_endpoint)
    resolver_src = inspect.getsource(resolve_generation_endpoint)
    assert "os.environ" not in resolver_src
    assert "is not a routable backend" in resolver_src


@pytest.mark.unit
def test_no_llm_endpoint_env_in_canonical_handler() -> None:
    """No ``os.environ["LLM_*"]`` endpoint reads in the canonical handler.

    SEA's kafka_runner read ``KAFKA_BOOTSTRAP_SERVERS`` from ``os.environ`` and
    the registry seeded ``LLM_*`` endpoints. The canonical handler must not read
    any endpoint URL from an ``LLM_*`` environment variable.
    """
    # Match a real os.environ access keyed by an LLM_* var, e.g.
    #   os.environ["LLM_CODER_URL"]  or  os.environ.get("LLM_CODER_URL")
    # Prose mentions of LLM_CODER_URL in docstrings/comments (explaining the
    # design intentionally does NOT read such a var) are allowed.
    env_read = re.compile(r"""os\.environ(?:\.get)?\(\s*['"]LLM_""")
    assert not env_read.search(_HANDLER_SRC), (
        "canonical handler must not read an LLM_* endpoint env var; "
        "endpoints resolve from the routing-authority contract"
    )


@pytest.mark.unit
def test_canonical_handler_does_not_import_kafka_client() -> None:
    """No raw Kafka client in the canonical node (kafka_runner.py capability).

    SEA's ``kafka_runner.py`` owned a raw ``KafkaConsumer``/``KafkaProducer``
    daemon loop with SIGINT/SIGTERM handlers. The canonical node owns NONE of
    that - transport/lifecycle is the runtime's job; the node only declares its
    topics in the contract and implements ``handle``.
    """
    assert "from kafka import" not in _HANDLER_SRC
    assert "KafkaConsumer" not in _HANDLER_SRC
    assert "KafkaProducer" not in _HANDLER_SRC
    assert "signal.signal" not in _HANDLER_SRC


# ---------------------------------------------------------------------------
# Migration boundary doc must record the ROUTE closure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_route_migration_boundary_doc_exists() -> None:
    """The ROUTE closure is documented as a capability->canonical-home boundary."""
    doc = _repo_root() / "docs" / "migrations" / "sea-generation-pipeline-routing.md"
    assert doc.is_file(), f"missing migration boundary doc: {doc}"
    text = doc.read_text()
    assert "consumer.py" in text
    assert "consumer_local.py" in text
    assert "kafka_runner.py" in text
    assert "node_generation_consumer" in text
    assert "routing authority" in text
