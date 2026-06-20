# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13294 — this corpus's fixtures ARE agent-left marker
# literals (the tokens this scanner-under-test must flag); the literals are the subject.
# TODO_FORMAT_EXEMPT: OMN-13294 the marker tokens below are corpus fixture data, not real work items
"""Acceptance corpus for the TODO/FIXME/HACK-marker mechanical scanner (OMN-13294, G2).

Ground truth: ``node_aislop_sweep`` ``_TODO_PATTERN`` (``\\b(TODO|FIXME|HACK)\\b``)
already encodes the invariant "an agent-left ``TODO`` / ``FIXME`` / ``HACK``
marker in shipped source is unfinished work that must not merge silently". The
aislop sweep is ADVISORY; this corpus turns the same invariant into the
acceptance authority for a generated COMPUTE validator that BLOCKS.

The marker is matched on a whole-word boundary so the scanner must flag the
token only when it stands alone (``# TODO: wire this``), not when it is a
substring of a larger identifier (``TODOLIST``, ``mastodon``). That word-boundary
behaviour is exactly what the adversarial clean mutations below pin.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture:
``FIXME`` and ``HACK`` in place of ``TODO``, and a marker embedded mid-line after
code. They prove the scanner generalises the marker set rather than memorising
the literal ``TODO``.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["TODO_MARKER_CORPUS"]


TODO_MARKER_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base case: a standalone marker token in a comment ---
        ModelCorpusFixture(
            fixture_id="v-base-todo",
            source="# TODO: resolve the endpoint from the contract before merge",
            description="standalone TODO marker in a comment — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-fixme",
            source="# FIXME: this loop double-counts retries",
            description="standalone FIXME marker in a comment — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-hack",
            source="    value = raw  # HACK: bypassing validation for now",
            description="HACK marker mid-line after code — must still flag",
            mutation_of="v-base-todo",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-todo-no-colon",
            source="# TODO wire the projection consumer",
            description="TODO marker without a trailing colon — must still flag",
            mutation_of="v-base-todo",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-fixme-bracketed",
            source="raise NotImplementedError  # FIXME(jonah) implement the handler",
            description="FIXME marker with parenthesised owner — must still flag",
            mutation_of="v-base-fixme",
        ),
    ],
    clean_fixtures=[
        # --- finished code with no marker at all ---
        ModelCorpusFixture(
            fixture_id="c-base-clean-code",
            source="result = handler.handle(envelope)  # resolves via DI container",
            description="ordinary comment, no TODO/FIXME/HACK marker — clean",
        ),
        # --- adversarial clean mutation: marker token as an identifier SUBSTRING ---
        ModelCorpusFixture(
            fixture_id="c-mut-todo-substring",
            source='TODOLIST_TABLE = "user_todolist"',
            description=(
                "TODO is a substring of the identifier TODOLIST, not a standalone "
                "word-boundary marker — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
        # --- adversarial clean mutation: 'hack' inside an unrelated word ---
        ModelCorpusFixture(
            fixture_id="c-mut-hack-substring",
            source='dataset = "mastodon-hackathon-corpus"',
            description=(
                "the letters 'hack' appear inside 'hackathon' (lowercase, no word "
                "boundary on the uppercase marker) — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
        # --- adversarial clean mutation: lowercase 'todo' in prose, not the marker ---
        ModelCorpusFixture(
            fixture_id="c-mut-lowercase-prose",
            source='DESC = "items still to do are tracked in Linear"',
            description=(
                "the marker pattern is the uppercase token TODO; the prose phrase "
                "'to do' is not the marker — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
    ],
)
