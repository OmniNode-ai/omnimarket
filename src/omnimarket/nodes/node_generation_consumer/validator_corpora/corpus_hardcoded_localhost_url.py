# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13294 — this corpus's fixtures ARE hardcoded localhost/loopback
# URL violations the generated scanner-under-test must flag; the literals are the subject.
# onex-allow-file OMN-13294 reason="this acceptance corpus's entire subject is hardcoded localhost/loopback URL literals the generated scanner must flag; per-line markers would obscure the fixtures"
"""Acceptance corpus for the hardcoded-localhost-URL mechanical scanner (OMN-13294, G2).

Ground truth: ``node_aislop_sweep`` ``_HARDCODED_CONFIG_PATTERNS`` already encodes
the invariant "a quoted ``http(s)://localhost`` or ``http(s)://127.0.0.1`` URL
literal in source is a portability / config-leak bug" (an endpoint that only
resolves on the author's box, never in CI / containers / the .201 server). It is
an ADVISORY sweep, not a blocking pre-commit gate; CLAUDE.md "All URLs from
contracts only" (epic OMN-12803) names the durable rule: every endpoint URL is
resolved from a contract / routing authority, never a code literal. This corpus
turns that invariant into the acceptance authority for a generated COMPUTE
validator that BLOCKS.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture:
the ``https`` scheme variant, a ``127.0.0.1`` loopback in place of the
``localhost`` hostname, and a port-suffixed form. They prove the scanner
generalises the loopback-URL invariant rather than memorising a curated set.

Clean fixtures pin the boundary: a real LAN/public host in a URL is a DIFFERENT
finding (owned by other scanners) and must stay clean here; the bare word
``localhost`` not inside a URL literal is not a hardcoded-URL violation; a
contract-sourced endpoint reference carries no URL literal; and an
``onex-allow-internal-ip``-suppressed line is the escape hatch.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["HARDCODED_LOCALHOST_URL_CORPUS"]


HARDCODED_LOCALHOST_URL_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base cases: localhost + loopback, each scheme, the canonical shape ---
        ModelCorpusFixture(
            fixture_id="v-base-localhost-http",
            source='BASE_URL = "http://localhost:8000/v1/chat/completions"',
            description="http://localhost URL literal — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-loopback-http",
            source='ENDPOINT = "http://127.0.0.1:8085/health"',
            description="http://127.0.0.1 loopback URL literal — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-localhost-https",
            source='BASE_URL = "https://localhost:8443/v1/models"',
            description="mutated to the https scheme on localhost — must still flag",
            mutation_of="v-base-localhost-http",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-loopback-https",
            source='URL = "https://127.0.0.1/api"',
            description="mutated to https loopback, no explicit port — must still flag",
            mutation_of="v-base-loopback-http",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-localhost-bare-slash",
            source="DSN = 'http://localhost/metrics'",
            description="single-quoted localhost URL, path-only (no port) — must still flag",
            mutation_of="v-base-localhost-http",
        ),
    ],
    clean_fixtures=[
        # --- a real host in a URL is a DIFFERENT scanner's finding: clean here ---
        ModelCorpusFixture(
            fixture_id="c-base-public-host-url",
            source='DOCS = "https://docs.omninode.ai/v1"',  # url-authority-ok: OMN-13294 corpus fixture data, not a real endpoint — the scanner-under-test must NOT flag it
            description="public host URL (not localhost/loopback) — must stay clean",
        ),
        # --- contract-sourced endpoint, no URL literal at all ---
        ModelCorpusFixture(
            fixture_id="c-base-contract-ref",
            source='endpoint = resolve_endpoint("local-coder")',
            description="endpoint resolved from routing authority, no URL literal — clean",
        ),
        # --- adversarial clean mutation: bare 'localhost' word, NOT a URL literal ---
        ModelCorpusFixture(
            fixture_id="c-mut-bare-localhost-word",
            source='COMMENT = "binds to localhost in the dev profile only"',
            description=(
                "the word localhost inside prose, not an http(s):// URL literal "
                "— must stay clean"
            ),
            mutation_of="c-base-public-host-url",
        ),
        # --- adversarial clean mutation: a hostname that merely CONTAINS 'localhost' ---
        ModelCorpusFixture(
            fixture_id="c-mut-localhost-substring-host",
            source='URL = "https://localhost-mirror.omninode.ai/v1"',  # url-authority-ok: OMN-13294 corpus fixture data, not a real endpoint — adversarial clean case the scanner-under-test must NOT flag
            description=(
                "a real public host whose name contains the substring 'localhost' "
                "— the loopback host is localhost exactly, so this must stay clean"
            ),
            mutation_of="c-base-public-host-url",
        ),
        # --- suppression escape hatch ---
        ModelCorpusFixture(
            fixture_id="c-mut-suppressed",
            source='URL = "http://localhost:8000"  # onex-allow-internal-ip approved local fixture',
            description=(
                "localhost URL literal on a line carrying the onex-allow-internal-ip "
                "marker — suppressed, must stay clean"
            ),
            mutation_of="c-base-public-host-url",
        ),
    ],
)
