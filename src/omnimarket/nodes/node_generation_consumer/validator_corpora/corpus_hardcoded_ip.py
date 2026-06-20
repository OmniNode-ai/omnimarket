# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13294 — this corpus's fixtures ARE hardcoded private-IP
# violations the generated scanner-under-test must flag; the literals are the subject.
# onex-allow-internal-ip OMN-13294 reason="corpus fixtures are intentional hardcoded private-IP violations the scanner-under-test must flag"
# onex-allow-file OMN-13294 reason="this acceptance corpus's entire subject is hardcoded private-IP literals the generated scanner must flag; the .201 endpoint fixture mirrors the live generation backend"
# onex-allow-file-internal-ip OMN-13294 reason="this acceptance corpus's entire subject is LAN-IP literals the generated scanner must flag; per-line markers would obscure the fixtures"
"""Acceptance corpus for the hardcoded-private-IP mechanical scanner (OMN-13294, G2).

Ground truth: ``node_aislop_sweep`` ``_HARDCODED_CONFIG_PATTERNS`` already encodes
the invariant "a quoted RFC1918 private IP literal in source is a portability /
config-leak bug" (ranges ``192.168.``, ``10.``, ``172.16``-``172.31``), but it is
an ADVISORY sweep, not a blocking pre-commit gate. CLAUDE.md Rule 6 names the
suppression marker ``# onex-allow-internal-ip``. This corpus turns that invariant
into the acceptance authority for a generated COMPUTE validator that BLOCKS.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture:
a different octet (``192.168.1.5`` -> ``192.168.99.250``), a different RFC1918
band (``10.`` -> ``172.20.``), and an ``https://`` prefix. They prove the scanner
generalises the RFC1918 invariant rather than memorising a curated set.

Clean fixtures pin the boundary: a PUBLIC IP (``8.8.8.8``) is NOT private and must
stay clean; a SemVer string (``1.10.172.0`` shape) and a non-network dotted-quad
are not network literals; an ``onex-allow-internal-ip``-suppressed line is the
escape hatch; and a contract-sourced endpoint reference (no literal IP) is clean.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["HARDCODED_IP_CORPUS"]


HARDCODED_IP_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base cases: each RFC1918 band, quoted, the canonical shape ---
        ModelCorpusFixture(
            fixture_id="v-base-192-168",
            source='HOST = "192.168.86.201"',  # onex-allow-internal-ip OMN-13294 corpus fixture: the literal the scanner must flag
            description="192.168/16 private IP literal (the .201 server) — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-10",
            source='BROKER = "10.0.0.42"',
            description="10/8 private IP literal — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-172",
            source='DB = "172.16.5.10"',
            description="172.16/12 private IP literal — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-192-octet",
            source='HOST = "192.168.99.250"',
            description="mutated octets in the same 192.168 band — must still flag",
            mutation_of="v-base-192-168",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-172-band-edge",
            source='ADDR = "172.31.255.254"',
            description="mutated to the upper edge of the 172.16-31 band — must flag",
            mutation_of="v-base-172",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-url-prefixed",
            source='ENDPOINT = "https://192.168.86.201:8000/v1/chat/completions"',  # onex-allow-internal-ip OMN-13294 corpus fixture: the literal the scanner must flag
            description="private IP embedded in an https URL literal — must still flag",
            mutation_of="v-base-192-168",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-10-single-quote",
            source="REDIS = '10.10.10.10'",
            description="mutated to single-quoted 10/8 literal — must still flag",
            mutation_of="v-base-10",
        ),
    ],
    clean_fixtures=[
        # --- public IP is NOT private: the precise boundary the scanner must hold ---
        ModelCorpusFixture(
            fixture_id="c-base-public-ip",
            source='DNS = "8.8.8.8"',
            description="public IP (not RFC1918) — must stay clean",
        ),
        # --- contract-sourced endpoint, no literal IP ---
        ModelCorpusFixture(
            fixture_id="c-base-contract-ref",
            source='endpoint_ref = resolve_endpoint("local-coder")',
            description="endpoint resolved from routing authority, no IP literal — clean",
        ),
        # --- adversarial clean mutation: a SemVer-shaped dotted token, NOT a 4-octet IP ---
        ModelCorpusFixture(
            fixture_id="c-mut-semver",
            source='VERSION = "1.10.172.0"',
            description=(
                "version string shaped like dotted-quad but not an RFC1918 band "
                "(leading 1.) — must stay clean"
            ),
            mutation_of="c-base-public-ip",
        ),
        # --- adversarial clean mutation: near-miss 172 OUTSIDE the private band ---
        ModelCorpusFixture(
            fixture_id="c-mut-172-public",
            source='HOST = "172.15.0.1"',
            description=(
                "172.15 is BELOW the 172.16-31 private band (public) — must stay clean"
            ),
            mutation_of="c-base-public-ip",
        ),
        # --- suppression escape hatch ---
        ModelCorpusFixture(
            fixture_id="c-mut-suppressed",
            source='HOST = "192.168.86.201"  # onex-allow-internal-ip approved test fixture',
            description=(
                "private IP literal on a line carrying the onex-allow-internal-ip "
                "marker — suppressed, must stay clean"
            ),
            mutation_of="c-base-public-ip",
        ),
    ],
)
