# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13509 — this corpus's fixtures ARE sibling dependency pins
# (git rev / branch=main) the generated scanner-under-test must classify; the
# literals are the subject.
"""Acceptance corpus for the sibling-pin-hygiene mechanical scanner (OMN-13509, G2).

Ground truth: the #2071 / OMN-13507 saga — a feature PR pinned a sibling
dependency (``omnibase-core`` / ``omnibase-spi`` / ``omnibase-compat``) to a git
``rev`` (or a ``branch=main``) that had DIVERGED from that sibling's ``dev`` line.
The pin compiled and tests passed locally, but the pinned revision was not an
ancestor of the sibling's ``dev`` HEAD, so it dragged in (or stranded) work that
was never on the integration line. The recurrence fix is a PR-time gate that
BLOCKS any sibling pin whose pinned commit is NOT an ancestor of that sibling's
``dev`` HEAD.

Architecture seam (why this is corpus-gateable as a PURE text scanner):
git ancestry is resolved at the EFFECT boundary (the runner runs
``git merge-base --is-ancestor <rev> <sibling-dev-head>`` against the local
canonical sibling clone) and annotates each pin line it loads with the resolved
fact as a trailing structured comment::

    omnibase-core = { git = "...", rev = "abc123" }  # pin-ancestry: ancestor
    omnibase-core = { git = "...", rev = "def456" }  # pin-ancestry: orphan

The COMPUTE scanner is then a pure, deterministic function over that annotated
text: it finds each sibling pin (rev= / @rev / ?rev= / branch=) and FLAGS the
line when the resolved ancestry annotation is anything other than ``ancestor``
(``orphan`` = diverged/off-dev-line, ``unknown`` = unresolved → fail closed).
A pin annotated ``ancestor`` is clean. This keeps git I/O out of the COMPUTE
node (purity preserved) while the corpus — not the LLM — remains the acceptance
authority (OMN-13289): the scanner is accepted iff it flags every diverged /
orphan / unknown pin and passes every ancestor pin.

The three sibling distributions guarded are exactly the ones the layering rule
names: ``omnibase-core``, ``omnibase-spi``, ``omnibase-compat``.

Mutation cases (``mutation_of``) are adversarial perturbations of a base
fixture: the same divergence expressed in the PEP-508 ``@rev`` form, in the
``uv.lock`` ``?rev=`` form, and as a ``branch=main`` pin that has diverged. They
prove the scanner generalises "non-ancestor sibling pin" across the pin syntaxes
rather than memorising one shape.

Clean fixtures pin the boundary: an ``ancestor`` pin in each syntax is clean; a
non-sibling git pin (some third-party package) is out of scope and clean; and a
version-range pin (``omnibase-core>=0.44``, no git rev at all) is not a git pin
and must stay clean.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["PIN_HYGIENE_CORPUS"]


PIN_HYGIENE_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base cases: a diverged/orphan sibling pin in each canonical syntax ---
        ModelCorpusFixture(
            fixture_id="v-base-pyproject-rev",
            source=(
                'omnibase-core = { git = "https://github.com/OmniNode-ai/'
                'omnibase_core.git", rev = "def4560000000000000000000000000000000000" }'
                "  # pin-ancestry: orphan"
            ),
            description=(
                "pyproject [tool.uv.sources] rev= sibling pin whose commit is NOT an "
                "ancestor of omnibase-core dev HEAD (orphan) — must flag"
            ),
        ),
        ModelCorpusFixture(
            fixture_id="v-base-spi-rev",
            source=(
                'omnibase-spi = { git = "https://github.com/OmniNode-ai/'
                'omnibase_spi.git", rev = "0123456789abcdef0123456789abcdef01234567" }'
                "  # pin-ancestry: orphan"
            ),
            description="omnibase-spi rev= pin off the dev line (orphan) — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-compat-rev",
            source=(
                'omnibase-compat = { git = "https://github.com/OmniNode-ai/'
                'omnibase_compat.git", rev = "fedcba9876543210fedcba9876543210fedcba98" }'
                "  # pin-ancestry: orphan"
            ),
            description="omnibase-compat rev= pin off the dev line (orphan) — must flag",
        ),
        # --- adversarial mutation cases (same divergence, different pin syntax) ---
        ModelCorpusFixture(
            fixture_id="v-mut-pep508-at-rev",
            source=(
                '    "omnibase-core @ git+https://github.com/OmniNode-ai/'
                'omnibase_core.git@def4560000000000000000000000000000000000",'
                "  # pin-ancestry: orphan"
            ),
            description=(
                "same orphan core commit expressed in the PEP-508 @rev form — must "
                "still flag"
            ),
            mutation_of="v-base-pyproject-rev",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-uvlock-qrev",
            source=(
                '    { name = "omnibase-core", git = "https://github.com/OmniNode-ai/'
                'omnibase_core.git?rev=def4560000000000000000000000000000000000" },'
                "  # pin-ancestry: orphan"
            ),
            description=(
                "same orphan core commit expressed in the uv.lock ?rev= form — must "
                "still flag"
            ),
            mutation_of="v-base-pyproject-rev",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-branch-main-diverged",
            source=(
                'omnibase-spi = { git = "https://github.com/OmniNode-ai/'
                'omnibase_spi.git", branch = "main" }  # pin-ancestry: orphan'
            ),
            description=(
                "branch=main sibling pin that has DIVERGED from dev (the #2071 shape) "
                "— main is NOT an ancestor of dev here, must flag"
            ),
            mutation_of="v-base-spi-rev",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-unknown-fail-closed",
            source=(
                'omnibase-core = { git = "https://github.com/OmniNode-ai/'
                'omnibase_core.git", rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }'
                "  # pin-ancestry: unknown"
            ),
            description=(
                "ancestry could not be resolved (rev not found in the local sibling "
                "clone) — fail CLOSED, must flag (unknown is not a pass)"
            ),
            mutation_of="v-base-pyproject-rev",
        ),
    ],
    clean_fixtures=[
        # --- ancestor pin in each syntax: the precise clean boundary ---
        ModelCorpusFixture(
            fixture_id="c-base-pyproject-rev-ancestor",
            source=(
                'omnibase-core = { git = "https://github.com/OmniNode-ai/'
                'omnibase_core.git", rev = "8ecb7efc17721dda2ce468b2e5051816ff8e89bc" }'
                "  # pin-ancestry: ancestor"
            ),
            description=(
                "core rev= pin that IS an ancestor of omnibase-core dev HEAD — clean"
            ),
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-pep508-at-rev-ancestor",
            source=(
                '    "omnibase-core @ git+https://github.com/OmniNode-ai/'
                'omnibase_core.git@8ecb7efc17721dda2ce468b2e5051816ff8e89bc",'
                "  # pin-ancestry: ancestor"
            ),
            description="ancestor core pin in PEP-508 @rev form — clean",
            mutation_of="c-base-pyproject-rev-ancestor",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-uvlock-qrev-ancestor",
            source=(
                '    { name = "omnibase-core", git = "https://github.com/OmniNode-ai/'
                'omnibase_core.git?rev=8ecb7efc17721dda2ce468b2e5051816ff8e89bc" },'
                "  # pin-ancestry: ancestor"
            ),
            description="ancestor core pin in uv.lock ?rev= form — clean",
            mutation_of="c-base-pyproject-rev-ancestor",
        ),
        # --- non-sibling git pin is OUT OF SCOPE, regardless of ancestry ---
        ModelCorpusFixture(
            fixture_id="c-mut-non-sibling-git-pin",
            source=(
                'some-thirdparty-lib = { git = "https://github.com/acme/'
                'thirdparty.git", rev = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" }'
                "  # pin-ancestry: orphan"
            ),
            description=(
                "a git pin for a NON-sibling package — out of scope even when annotated "
                "orphan, must stay clean (only omnibase-core/spi/compat are guarded)"
            ),
            mutation_of="c-base-pyproject-rev-ancestor",
        ),
        # --- a version-range sibling pin (no git rev at all) is not a git pin ---
        ModelCorpusFixture(
            fixture_id="c-mut-version-range-pin",
            source='    "omnibase-core>=0.44.0,<0.47.0",',
            description=(
                "sibling pinned by published version range, no git rev — not a git pin, "
                "must stay clean"
            ),
            mutation_of="c-base-pyproject-rev-ancestor",
        ),
    ],
)
