# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13294 — this corpus's fixtures ARE hardcoded onex.* topic-string
# violations the generated scanner-under-test must flag; the literals are the subject.
"""Acceptance corpus for the hardcoded-topic-string mechanical scanner (OMN-13294, G2).

Ground truth: ``node_aislop_sweep`` ``_HARDCODED_TOPIC_PATTERN`` already encodes
the invariant "a quoted ``onex.<segment>.<segment>.<segment>`` topic literal in
handler source is a contract-drift bug" — topics must be declared in
``contract.yaml`` and resolved through it, never pasted as a string literal in a
handler (omnimarket CLAUDE.md: "Keep event topics declared in contract.yaml;
avoid hardcoded topic strings in handlers"; memory ``feedback_bus_is_the_transport``).
The aislop sweep is ADVISORY; this corpus turns the same invariant into the
acceptance authority for a generated COMPUTE validator that BLOCKS.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture:
a different topic suffix, a single-quoted form, and a different leading segment
(``onex.delegation.*`` vs ``onex.generation.*``). They prove the scanner
generalises the dotted ``onex.*`` topic shape rather than memorising one topic.

Clean fixtures pin the boundary: a topic name resolved from the contract (no
literal) is the correct pattern and must stay clean; a non-``onex`` dotted
package/module path (``omnimarket.nodes.foo``) is not a topic literal; and a
two-segment ``onex.foo`` string is below the topic shape (the pattern requires at
least ``onex.<a>.<b>.<c>``) so it must stay clean.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["HARDCODED_TOPIC_CORPUS"]


HARDCODED_TOPIC_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base cases: canonical onex.<a>.<b>.<c> topic literals ---
        ModelCorpusFixture(
            fixture_id="v-base-generation-topic",
            source='TOPIC = "onex.generation.benchmark.completed"',
            description="hardcoded onex.* topic literal in source — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-publish-call",
            source='await bus.publish("onex.delegation.attempt.started", env)',
            description="hardcoded onex.* topic passed inline to publish() — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-single-quote",
            source="SWEEP_TOPIC = 'onex.aislop.sweep.completed'",
            description="mutated to a single-quoted onex.* topic literal — must still flag",
            mutation_of="v-base-generation-topic",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-other-domain",
            source='RESULT_TOPIC = "onex.review.verdict.posted"',
            description="mutated to a different onex.* domain/suffix — must still flag",
            mutation_of="v-base-generation-topic",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-deeper-segments",
            source='EVT = "onex.runtime.node.deploy.requested"',
            description="mutated to a deeper (5-segment) onex.* topic — must still flag",
            mutation_of="v-base-publish-call",
        ),
    ],
    clean_fixtures=[
        # --- topic resolved from the contract: the correct pattern, clean ---
        ModelCorpusFixture(
            fixture_id="c-base-contract-topic",
            source='topic = self._contract.topics["benchmark_completed"]',
            description="topic resolved from contract.yaml, no string literal — clean",
        ),
        # --- a dotted module/package path that is NOT an onex.* topic ---
        ModelCorpusFixture(
            fixture_id="c-base-module-path",
            source="from omnimarket.nodes.node_generation_consumer.models import x",
            description="dotted python import path (not onex.* topic shape) — clean",
        ),
        # --- adversarial clean mutation: an onex.* string TOO SHORT to be a topic ---
        ModelCorpusFixture(
            fixture_id="c-mut-two-segment",
            source='NAMESPACE = "onex.core"',
            description=(
                "onex.core is two-segment, below the onex.<a>.<b>.<c> topic shape "
                "— must stay clean"
            ),
            mutation_of="c-base-contract-topic",
        ),
        # --- adversarial clean mutation: a non-onex dotted topic-shaped string ---
        ModelCorpusFixture(
            fixture_id="c-mut-non-onex-prefix",
            source='LEGACY = "kafka.cluster.broker.id"',
            description=(
                "dotted four-segment string but NOT prefixed onex. — not an onex "
                "topic literal, must stay clean"
            ),
            mutation_of="c-base-contract-topic",
        ),
    ],
)
