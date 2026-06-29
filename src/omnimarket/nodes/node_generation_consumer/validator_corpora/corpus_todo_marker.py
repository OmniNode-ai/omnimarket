# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance corpus for the unfinished-work-marker mechanical scanner (OMN-13294, G2).

Ground truth: ``node_aislop_sweep`` ``_TODO_PATTERN`` (a word-boundary match on the
three uppercase work-item tokens) already encodes the invariant "an agent-left
unfinished-work marker in shipped source is work that must not merge silently".
The aislop sweep is ADVISORY; this corpus turns the same invariant into the
acceptance authority for a generated COMPUTE validator that BLOCKS.

The marker is matched on a whole-word boundary so the scanner must flag the token
only when it stands alone (e.g. as a leading comment), not when it is a substring
of a larger identifier (``TODOLIST``, ``mastodon``). That word-boundary behaviour
is exactly what the adversarial clean mutations below pin.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

Implementation note: each fixture's marker token is assembled at module load
from ``_TODO`` / ``_FIXME`` / ``_HACK`` string parts rather than written as a
literal hashed comment. The fixture *value* the scanner receives still contains
the real hashed marker; keeping the literal token out of THIS source file is what
stops the marker gates (which scan this repo's own source) from flagging the
corpus that defines them — the corpus is the subject, not a real work item.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["TODO_MARKER_CORPUS"]

# Marker tokens assembled at module load so the literal hashed marker never
# appears verbatim in this source file (see the implementation note above). The
# assembled fixture sources DO carry the real marker the scanner must flag.
_HASH = "#"
_TODO = "TO" + "DO"
_FIXME = "FIX" + "ME"
_HACK = "HA" + "CK"


TODO_MARKER_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base case: a standalone marker token in a leading comment ---
        ModelCorpusFixture(
            fixture_id="v-base-todo",
            source=f"{_HASH} {_TODO}: resolve the endpoint from the contract before merge",
            description="standalone work-item marker in a comment — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-fixme",
            source=f"{_HASH} {_FIXME}: this loop double-counts retries",
            description="standalone fix-me marker in a comment — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-hack",
            source=f"    value = raw  {_HASH} {_HACK}: bypassing validation for now",
            description="hack marker mid-line after code — must still flag",
            mutation_of="v-base-todo",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-todo-no-colon",
            source=f"{_HASH} {_TODO} wire the projection consumer",
            description="marker without a trailing colon — must still flag",
            mutation_of="v-base-todo",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-fixme-bracketed",
            source=f"raise NotImplementedError  {_HASH} {_FIXME}(jonah) implement the handler",
            description="fix-me marker with parenthesised owner — must still flag",
            mutation_of="v-base-fixme",
        ),
    ],
    clean_fixtures=[
        # --- finished code with no marker at all ---
        ModelCorpusFixture(
            fixture_id="c-base-clean-code",
            source=f"result = handler.handle(envelope)  {_HASH} resolves via DI container",
            description="ordinary comment, no work-item marker — clean",
        ),
        # --- adversarial clean mutation: marker token as an identifier SUBSTRING ---
        ModelCorpusFixture(
            fixture_id="c-mut-todo-substring",
            source=f'{_TODO}LIST_TABLE = "user_{_TODO.lower()}list"',
            description=(
                "the marker is a substring of the identifier (no word boundary on "
                "the uppercase token) — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
        # --- adversarial clean mutation: 'hack' inside an unrelated lowercase word ---
        ModelCorpusFixture(
            fixture_id="c-mut-hack-substring",
            source='dataset = "mastodon-hacksaw-corpus"',
            description=(
                "the letters of the marker appear lowercase inside 'hacksaw' "
                "(no uppercase word-boundary marker) — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
        # --- adversarial clean mutation: lowercase prose, not the uppercase marker ---
        ModelCorpusFixture(
            fixture_id="c-mut-lowercase-prose",
            source='DESC = "items still to do are tracked in Linear"',
            description=(
                "the marker is the uppercase token; the prose phrase 'to do' is "
                "not the marker — must stay clean"
            ),
            mutation_of="c-base-clean-code",
        ),
    ],
)
