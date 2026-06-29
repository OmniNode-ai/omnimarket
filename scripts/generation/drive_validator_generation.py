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
    "hardcoded-localhost-url": (
        "Generate a Python validator handler that scans source text for hardcoded "
        "localhost / loopback URL literals and returns the violations. The handler "
        "must define `def handle(input_data):` reading the source text from "
        "input_data['source']. Scan line by line. Flag any quoted URL literal whose "
        "host is exactly 'localhost' or '127.0.0.1' behind an http:// or https:// "
        "scheme, e.g. \"http://localhost:8000/v1\" or 'https://127.0.0.1/api'. The "
        "host must be exactly localhost or 127.0.0.1 — a public host whose NAME "
        "merely contains the substring 'localhost' (e.g. https://localhost-mirror."
        "example.com) must NOT be flagged, and a public URL like "
        "https://docs.example.com must NOT be flagged. The bare word 'localhost' in "
        "prose that is not inside an http(s):// URL literal must NOT be flagged. Skip "
        "any line containing the marker 'onex-allow-internal-ip'. Return a dict "
        "{'findings': [...]} where each finding describes the line and the matched "
        "URL. Use only the Python standard library (re). Do not read files, the "
        "network, env vars, or the clock."
    ),
    "hardcoded-topic-string": (
        "Generate a Python validator handler that scans source text for hardcoded "
        "ONEX event-topic string literals and returns the violations. The handler "
        "must define `def handle(input_data):` reading the source text from "
        "input_data['source']. Scan line by line. Flag any quoted string literal "
        "that is an onex topic of the shape onex.<segment>.<segment>.<segment> (at "
        "least three dot-separated lowercase segments after the onex prefix), e.g. "
        "\"onex.generation.benchmark.completed\" or 'onex.delegation.attempt.started'. "
        'A two-segment string like "onex.core" is BELOW the topic shape and must '
        "NOT be flagged. A dotted python import path like "
        "omnimarket.nodes.node_x.models or a non-onex dotted string like "
        '"kafka.cluster.broker.id" must NOT be flagged. Return a dict '
        "{'findings': [...]} where each finding describes the line and the matched "
        "topic. Use only the Python standard library (re). Do not read files, the "
        "network, env vars, or the clock."
    ),
    "todo-fixme-marker": (
        "Generate a Python validator handler that scans source text for agent-left "
        "TODO / FIXME / HACK markers and returns the violations. The handler must "
        "define `def handle(input_data):` reading the source text from "
        "input_data['source']. Scan line by line. Flag any line containing the "
        "uppercase token TODO, FIXME, or HACK as a standalone whole word (word "
        "boundary on both sides), as it appears in a code comment. The token must be "
        "a standalone word: a substring inside a larger identifier such as TODOLIST, "
        "or the letters of HACK inside 'hackathon', or the lowercase prose phrase "
        "'to do', must NOT be flagged. "
        "Return a dict {'findings': [...]} where each finding describes the line and "
        "the matched marker. Use only the Python standard library (re). Do not read "
        "files, the network, env vars, or the clock."
    ),
    "no-faked-boundary": (
        "Generate a Python validator handler that scans source text for FAKES of "
        "the platform's own inference / routing / dispatch boundary and returns the "
        "violations. The handler must define `def handle(input_data):` reading the "
        "source text from input_data['source']. Scan line by line. Flag a line when "
        "ANY of these patterns appears: "
        "(1) a class definition whose class name contains 'Fake', 'Stub', or 'Mock' "
        "AND that subclasses (has in its parenthesised bases) an identifier ending "
        "in one of: InferenceAdapter, Bridge, Router, RoutingResolver, or "
        "containing 'Dispatch' — e.g. `class _FakeBridge(ModelInferenceAdapter):` or "
        "`class _StubInferenceRouter(ModelInferenceAdapter):`. Do NOT flag a class "
        "that subclasses a base which is NOT one of those inference/routing/dispatch "
        "boundary identifiers — e.g. `class MockServiceHub(MixinServiceRegistry):` "
        "(a service-registry mixin) is a test harness, not a fake of the inference "
        "boundary, and must NOT be flagged; "
        "(2) a patch of the real HTTP egress: the line contains patch( (in any form "
        '— patch("..."), mock.patch(...), or a @patch(...) decorator) whose target '
        'string is "httpx.Client" or "httpx.AsyncClient"; '
        "(3) an assignment of the form `<target> = MagicMock()` or "
        "`<target> = AsyncMock()` where the assignment TARGET (the text to the LEFT "
        "of the '=', which may include a 'self.' prefix) contains any of the "
        "substrings 'inference', 'bridge', 'router', or 'dispatch' "
        "(case-insensitive). Concretely, flag a line that matches the regex "
        "`(inference|bridge|router|dispatch)\\w*\\s*=\\s*(MagicMock|AsyncMock)\\(` "
        "case-insensitively — this catches `inference_bridge = MagicMock()` and "
        "`self.router = AsyncMock()`. Both of those lines MUST be flagged; "
        "(4) a completion keyword argument whose VALUE is derived from the prompt "
        "variable rather than a recorded string literal — i.e. `completion=prompt` "
        '(the bare identifier prompt) or `completion=f"...{prompt}..."` (an '
        "f-string that interpolates {prompt}). "
        "Do NOT flag CLEAN lines: a completion set to a plain quoted string literal "
        "with no f-string interpolation of prompt (e.g. "
        '`completion="The capital of France is Paris."` or even '
        "`completion=\"Answer the prompt carefully.\"` where 'prompt' is just a word "
        "inside the recorded literal, not the variable) must NOT be flagged; a real "
        "adapter usage such as `RoutingResolvedJudgeInferenceAdapter(...)` must NOT "
        "be flagged; a real `EventBusInmemory()` usage must NOT be flagged; a class "
        "subclassing a non-boundary base such as "
        "`class MockServiceHub(MixinServiceRegistry):` must NOT be flagged; and a "
        "patch / mock of a genuinely external third-party service whose target is "
        'NOT httpx.Client/httpx.AsyncClient (e.g. patch("slack_sdk.WebClient"), '
        '@patch("boto3.client")) must NOT be flagged. '
        "Return a dict {'findings': [...]} where each finding describes the line and "
        "the matched pattern. Use only the Python standard library (re). Do not read "
        "files, the network, env vars, or the clock."
    ),
    "doc-content-scan": (
        "Generate a Python validator handler that scans DOCUMENTATION source text "
        "for local-environment traces and Linear ticket references, and returns the "
        "violations. The handler must define `def handle(input_data):` reading the "
        "source text from input_data['source']. Scan line by line. FLAG a line when "
        "ANY of these appears: "
        "(1) a hardcoded RFC1918 private IPv4 literal — first octet 192 and second "
        "168 (192.168.x.x), or first octet 10 (10.x.x.x), or first octet 172 with "
        "second octet 16 through 31 (172.16.x.x-172.31.x.x). A documentation-reserved "
        "RFC5737 address (192.0.2.x, 198.51.100.x, 203.0.113.x) and the loopback "
        "127.0.0.1 must NOT be flagged; "
        "(2) a `.201` or `.200` host shorthand referring to a host — a dot followed "
        "by exactly 201 or 200 as a standalone token (preceded by whitespace or start "
        "of line, NOT preceded by a digit). The text `.201` in `deployed to .201` or "
        "`ssh x@.201` must be flagged, but a decimal like `0.200` or a SemVer patch "
        "like `v1.201.0` (where the .201/.200 is preceded by a digit) must NOT be "
        "flagged; "
        "(3) a personal absolute path beginning `/Users/<name>/` or `/home/<name>/` "
        "(a real user home path). A portable form ($OMNI_HOME, ${ONEX_HOST}, "
        "Path.home()) must NOT be flagged; "
        "(4) an ssh invocation of the shape `ssh <user>@<host>` (the literal word ssh "
        "followed by user@host); "
        "(5) a personal e-mail address — a local-part followed by @ and a real mail "
        "domain such as gmail.com (e.g. someone@gmail.com). example.com is a reserved "
        "documentation domain and must NOT be flagged; "
        "(6) a Linear ticket reference of the shape OMN-<digits> (uppercase OMN, a "
        "hyphen, then one or more digits) appearing ANYWHERE on the line — in prose, "
        "in a parenthetical like (OMN-1234), in a markdown heading (## OMN-1234), in a "
        "list item (- OMN-1234), inside a link target URL, or inside a filename like "
        "OMN-1234-handoff.md. A token like OMNI_HOME or OMNINODE that is NOT the "
        "OMN-<digits> shape must NOT be flagged. "
        "Skip any line containing the marker 'doc-content-ok'. Also, if the ENTIRE "
        "source contains the marker 'doc-content-file-ok' anywhere, return no findings "
        "at all (the whole file is suppressed). "
        "Return a dict {'findings': [...]} where each finding describes the line and "
        "the matched text. Use only the Python standard library (re). Do not read "
        "files, the network, env vars, or the clock."
    ),
    "pin-hygiene": (
        "Generate a Python validator handler that scans dependency-pin source text "
        "for SIBLING git pins whose pinned commit is NOT an ancestor of that "
        "sibling's dev line, and returns the violations. The handler must define "
        "`def handle(input_data):` reading the source text from input_data['source']. "
        "Scan line by line. A SIBLING is one of exactly these three package names: "
        "omnibase-core, omnibase-spi, omnibase-compat (the hyphenated distribution "
        "names; also accept the underscore repo form omnibase_core/omnibase_spi/"
        "omnibase_compat that appears inside the git URL). A line is a sibling GIT "
        "PIN when it names one of those siblings AND carries a git revision in ANY of "
        'these three syntaxes: (a) pyproject [tool.uv.sources] form `rev = "<sha>"`, '
        "(b) PEP-508 form `@<sha>` after a git+https URL, (c) uv.lock form "
        '`?rev=<sha>`; OR it pins the sibling by a git `branch = "<name>"` (e.g. '
        'branch = "main"). Each such pin line carries a trailing resolved-ancestry '
        "annotation comment of the form `# pin-ancestry: <verdict>` where <verdict> "
        "is one of: ancestor, orphan, unknown (this annotation is injected by the "
        "caller after resolving git ancestry — the handler does NOT compute git "
        "ancestry itself, it reads the annotation). FLAG the line when the verdict is "
        "anything other than 'ancestor' — i.e. flag 'orphan' (the pinned commit "
        "diverged / is off the dev line) and flag 'unknown' (ancestry could not be "
        "resolved — fail CLOSED). A pin annotated `# pin-ancestry: ancestor` must NOT "
        "be flagged. "
        "Do NOT flag CLEAN lines: a git pin for a NON-sibling package (any package "
        "name that is not one of the three siblings, e.g. some-thirdparty-lib) must "
        "NOT be flagged even if annotated orphan — it is out of scope; a sibling "
        "pinned by a published VERSION RANGE with no git rev at all (e.g. "
        '`"omnibase-core>=0.44.0,<0.47.0"`) is not a git pin and must NOT be flagged; '
        "and any sibling git pin annotated `# pin-ancestry: ancestor` must NOT be "
        "flagged. "
        "Return a dict {'findings': [...]} where each finding describes the line, the "
        "sibling, and the resolved verdict. Use only the Python standard library "
        "(re). Do not read files, the network, env vars, or the clock."
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
