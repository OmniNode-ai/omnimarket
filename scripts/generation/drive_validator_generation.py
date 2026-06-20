# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""G2 generation driver — mass-produce mechanical scanner validators (OMN-13294).

Drives the PROVEN G1 loop (OMN-13293) for the G2 mechanical-scanner long tail:
for each acceptance corpus registered in
``node_generation_consumer.validator_corpora.CORPORA`` this script constructs a
``ModelNodeGenerationRequest`` carrying that ``validator_corpus`` and a task
description, runs it through the REAL ``HandlerGenerationConsumer`` against the
live local model (provider/served_model_id/endpoint resolved from the contract
``model_routing`` + bifrost overlay — the generator never selects its own model),
and reports the corpus-acceptance verdict.

Acceptance authority = the corpus, NOT the LLM (memory
``feedback_adversarial_receipts``): a generated scanner is reported ACCEPTED only
when ``benchmark.corpus_checked and benchmark.corpus_passed`` — i.e. the generated
handler flagged every ``violation_fixture`` and produced zero findings on every
``clean_fixture``, by deterministic execution in the hardened sandbox.

This is a driver / evidence harness, not a runtime node: it lives under
``scripts/`` (the EFFECT boundary that talks to the live model and writes the
provenance JSON). The artifact it accepts is then hand-landed in ``omnibase_core``
(producer != owner; build-time validators live in core).

Usage:
    uv run python -m scripts.generation.drive_validator_generation \\
        --validator hardcoded-private-ip \\
        --out docs/evidence/OMN-13294/hardcoded-private-ip.generation.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora import CORPORA

# Task descriptions per target validator. Each describes the mechanical scanner
# to generate in terms the local model can satisfy: a self-contained
# handle(input_data) reading input_data["source"] and returning a findings list.
# The description states the invariant; the CORPUS enforces correctness.
_TASK_DESCRIPTIONS: dict[str, str] = {
    "hardcoded-private-ip": (
        "Generate a Python validator handler that scans source text for hardcoded "
        "RFC1918 private IPv4 address literals and returns the violations. The "
        "handler must define `def handle(input_data):` reading the source text from "
        "input_data['source']. Scan line by line. Flag any quoted IPv4 literal whose "
        "first octet places it in a private range: 192.168.x.x, 10.x.x.x, or "
        "172.16.x.x through 172.31.x.x (the 172.16/12 block only). A public IP such "
        "as 8.8.8.8 or 172.15.0.1 must NOT be flagged. A version string like "
        "1.10.172.0 must NOT be flagged. Skip any line containing the marker "
        "'onex-allow-internal-ip'. Return a dict {'findings': [...]} where each "
        "finding is a dict describing the line and the matched IP. Use only the "
        "Python standard library (re). Do not read files, the network, env vars, or "
        "the clock."
    ),
}


async def _drive_one(validator: str, max_attempts: int) -> dict[str, object]:
    corpus = CORPORA[validator]
    task = _TASK_DESCRIPTIONS[validator]
    correlation_id = f"omn-13294-g2-{validator}-{uuid.uuid4().hex[:12]}"

    request = ModelNodeGenerationRequest(
        task_description=task,
        correlation_id=correlation_id,
        max_attempts=max_attempts,
        validator_corpus=corpus,
    )

    # No injected effect => the handler self-wires the real LLM inference effect
    # and resolves provider/model/endpoint from the contract routing authority.
    handler = HandlerGenerationConsumer()
    benchmark = await handler.handle(request)

    accepted = bool(benchmark.corpus_checked and benchmark.corpus_passed)
    return {
        "validator": validator,
        "correlation_id": correlation_id,
        "accepted": accepted,
        "provider": benchmark.provider,
        "model_id": benchmark.model_id,
        "endpoint_class": benchmark.endpoint_class,
        "routing_source": benchmark.routing_source,
        "resolved_endpoint": benchmark.resolved_endpoint,
        "attempt_count": benchmark.attempt_count,
        "usage_source": benchmark.usage_source.value,
        "contract_passed": benchmark.contract_passed,
        "corpus_checked": benchmark.corpus_checked,
        "corpus_passed": benchmark.corpus_passed,
        "corpus_errors": list(benchmark.corpus_errors),
        "violation_fixture_count": len(corpus.violation_fixtures),
        "clean_fixture_count": len(corpus.clean_fixtures),
        "contract_yaml": benchmark.contract_yaml,
        "handler_source": benchmark.handler_source,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drive_validator_generation")
    parser.add_argument(
        "--validator",
        choices=sorted(CORPORA),
        required=True,
        help="Which registered corpus / mechanical scanner to generate",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=10,
        help="Generation repair-loop attempts (routing authority escalates per attempt)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the full generation+acceptance provenance JSON here",
    )
    parsed = parser.parse_args(argv)

    result = asyncio.run(_drive_one(parsed.validator, parsed.max_attempts))

    if parsed.out is not None:
        parsed.out.parent.mkdir(parents=True, exist_ok=True)
        parsed.out.write_text(json.dumps(result, indent=2, sort_keys=True))

    # Print a compact verdict line (provenance JSON, minus the bulky source, to stdout).
    summary = {
        k: v for k, v in result.items() if k not in ("handler_source", "contract_yaml")
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
