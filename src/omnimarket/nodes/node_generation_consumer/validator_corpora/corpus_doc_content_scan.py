# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13568 — this corpus's fixtures ARE doc-content violations the
# generated scanner-under-test must flag; the literals are the subject.
# onex-allow-internal-ip OMN-13568 reason="corpus fixtures are intentional local-environment-trace violations the scanner-under-test must flag"
# onex-allow-file OMN-13568 reason="this acceptance corpus's entire subject is doc-content traces (LAN IP / personal path / ssh / email / OMN refs) the generated scanner must flag"
# onex-allow-file-internal-ip OMN-13568 reason="this acceptance corpus's entire subject is LAN-IP / host-shorthand literals the generated scanner must flag; per-line markers would obscure the fixtures"
# doc-content-file-ok OMN-13568 reason="this acceptance corpus IS the doc-content ground-truth fixtures; the literals are intentional"
"""Acceptance corpus for the documentation-content mechanical scanner (OMN-13568, G2).

Ground truth: a *documentation* file (``*.md`` / ``*.mdx`` / ``*.rst`` / ``*.txt``
/ ``*.adoc``) must not carry **local-environment traces** (RFC1918 LAN IP literals,
``.201`` / ``.200`` host shorthand, personal ``/Users/<user>`` or ``/home/<user>``
paths, ``ssh <user>@<host>`` lines, personal e-mail addresses) **or Linear ticket
references** (``OMN-<digits>``) in rendered prose — EXCEPT where exempt. The
invariant is the docs-facing union of CLAUDE.md Rule 6 (no hardcoded LAN IPs /
absolute paths) and the knowledge-base sanitizer (strip OMN ids / IPs / ``.201`` /
private URLs / e-mail) that already runs on the PUBLIC knowledge-base repo.

This corpus turns that invariant into the acceptance authority for a generated
COMPUTE validator. The corpus is the acceptance authority — NOT the LLM
(memory ``feedback_adversarial_receipts``). A generated scanner is accepted iff it
flags every ``violation_fixtures`` entry (>=1 finding) and produces zero findings
on every ``clean_fixtures`` entry, by deterministic execution in the hardened
sandbox.

Scope of THIS corpus (text-content layer): each fixture's ``source`` is a single
line of documentation text. The corpus therefore proves the **content-scanning**
logic — LAN-IP / host-shorthand / personal-path / ssh / e-mail / OMN-in-prose
detection plus the ``doc-content-ok`` / ``doc-content-file-ok`` suppression
markers. The PATH-based exemptions (``OMN-<digits>`` is allowed when the file lives
under ``onex_change_control/`` or ``contracts/``) are an EFFECT-boundary concern —
the corpus sandbox only ever sees ``input_data["source"]`` text, never a path — so
those exemptions are proven by the transplanted ``scan_source(content, path)`` unit
tests in omnibase_core, not by this text-only corpus.

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture
(a different LAN band, a different ticket-reference syntax, a different personal
path root) so the corpus cannot be passed by memorising a curated set
(OMN-13289 guard).

Clean fixtures pin the precise boundaries the scanner must hold: ``localhost`` /
``127.0.0.1`` are LEFT by decision (portable, non-identifying); the RFC5737
documentation IP ranges (``192.0.2.x`` / ``198.51.100.x`` / ``203.0.113.x``) and
``example.com`` are the canonical illustrative placeholders; env-var / portable
forms (``$OMNI_HOME`` / ``${ONEX_HOST}`` / ``Path.home()``) are the correct
substitute for a personal path; a SemVer / decimal token shaped like ``0.200`` or
``v1.201.0`` is NOT a ``.201`` host; and a suppression-marked line is the escape
hatch.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["DOC_CONTENT_SCAN_CORPUS"]


DOC_CONTENT_SCAN_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base cases: one per violation class, in documentation prose --------
        ModelCorpusFixture(
            fixture_id="v-base-lan-ip",
            source="The broker runs on 192.168.86.201 in the lab.",  # onex-allow-internal-ip OMN-13568 corpus fixture
            description="RFC1918 LAN IP literal in doc prose — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-host-shorthand",
            source="Deployed to .201 over the weekend.",
            description=".201 host shorthand referring to a host in prose — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-personal-path",
            source="Logs are written to /Users/jonah/Code/omni_home/run.log",
            description="personal /Users/<user> absolute path in a doc — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-ssh-line",
            source="Connect with `ssh jonah@192.168.86.201` then tail the logs.",  # onex-allow-internal-ip OMN-13568 corpus fixture
            description="ssh <user>@<host> line in a doc — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-personal-email",
            source="Questions go to jonah.neugass@gmail.com for now.",
            description="personal gmail address in a doc — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-omn-prose",
            source="This was fixed in OMN-13294 last sprint.",
            description="OMN-XXXX Linear ticket reference in prose — must flag",
        ),
        # --- adversarial mutation cases (must STILL flag) -----------------------
        ModelCorpusFixture(
            fixture_id="v-mut-lan-ip-10-band",
            source="Postgres listens on 10.0.0.5 inside the cluster.",
            description="mutated to the 10/8 RFC1918 band — must still flag",
            mutation_of="v-base-lan-ip",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-lan-ip-172-band",
            source="Valkey is reachable at 172.16.4.9 from the runners.",
            description="mutated to the 172.16/12 RFC1918 band — must still flag",
            mutation_of="v-base-lan-ip",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-host-shorthand-200",
            source="The Mac Studio is the .200 box.",
            description="mutated to .200 host shorthand — must still flag",
            mutation_of="v-base-host-shorthand",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-personal-path-home",
            source="Drop the artifact in /home/jonah/scratch/out.json overnight.",
            description="mutated to a /home/<user> personal path root — must still flag",
            mutation_of="v-base-personal-path",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-parenthetical",
            source="The renderer is the effect node (OMN-12822) on the bus.",
            description="OMN-XXXX as a parenthetical reference — must still flag",
            mutation_of="v-base-omn-prose",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-heading",
            source="## OMN-13567 doc content scan",
            description="OMN-XXXX in a markdown heading — must still flag",
            mutation_of="v-base-omn-prose",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-list-item",
            source="- See OMN-13568 for the corpus.",
            description="OMN-XXXX in a markdown list item — must still flag",
            mutation_of="v-base-omn-prose",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-link-target",
            source="Details: [the ticket](https://linear.app/omninode/issue/OMN-13569).",
            description="OMN-XXXX in a markdown link target URL — must still flag",
            mutation_of="v-base-omn-prose",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-filename",
            source="Evidence lives in OMN-13294-handoff.md at the repo root.",
            description="OMN-XXXX embedded in a referenced filename — must still flag",
            mutation_of="v-base-omn-prose",
        ),
    ],
    clean_fixtures=[
        # --- localhost / loopback LEFT by decision (portable, non-identifying) --
        ModelCorpusFixture(
            fixture_id="c-base-localhost",
            source="Open http://localhost:3000 to view the dashboard.",
            description="localhost URL — LEFT by decision, must stay clean",
        ),
        ModelCorpusFixture(
            fixture_id="c-base-loopback",
            source="The dev server binds 127.0.0.1 only.",
            description="127.0.0.1 loopback — LEFT by decision, must stay clean",
        ),
        # --- RFC5737 documentation IP ranges + example.com ----------------------
        ModelCorpusFixture(
            fixture_id="c-base-doc-ip-192-0-2",
            source="Point the client at 192.0.2.10 (TEST-NET-1) in the example.",
            description="RFC5737 192.0.2.x documentation IP — must stay clean",
        ),
        ModelCorpusFixture(
            fixture_id="c-base-example-com",
            source="Webhooks post to https://hooks.example.com/ingest.",
            description="example.com reserved doc domain — must stay clean",
        ),
        # --- portable / env-var forms (the correct substitute for a real path) --
        ModelCorpusFixture(
            fixture_id="c-base-env-var-path",
            source="Artifacts go under $OMNI_HOME/omni_worktrees/out.log.",
            description="$OMNI_HOME env-var path (portable) — must stay clean",
        ),
        # --- adversarial CLEAN mutations ----------------------------------------
        ModelCorpusFixture(
            fixture_id="c-mut-doc-ip-198-51-100",
            source="Try 198.51.100.42 (TEST-NET-2) for the second example.",
            description="RFC5737 198.51.100.x documentation IP — must stay clean",
            mutation_of="c-base-doc-ip-192-0-2",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-doc-ip-203-0-113",
            source="The third example uses 203.0.113.7 (TEST-NET-3).",
            description="RFC5737 203.0.113.x documentation IP — must stay clean",
            mutation_of="c-base-doc-ip-192-0-2",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-env-var-braced",
            source="Connect to ${ONEX_HOST} resolved from the overlay.",
            description="${ONEX_HOST} braced env-var form (portable) — must stay clean",
            mutation_of="c-base-env-var-path",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-path-home-call",
            source="Defaults resolve from Path.home() / '.omnibase'.",
            description="Path.home() portable resolution — must stay clean",
            mutation_of="c-base-env-var-path",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-decimal-not-host",
            source="The free tier prices at $0.200 per million tokens.",
            description=(
                "a decimal token shaped like .200 but NOT a host shorthand "
                "(preceded by a digit) — must stay clean"
            ),
            mutation_of="c-base-localhost",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-semver-not-host",
            source="Released as v1.201.0 on the changelog.",
            description=(
                "a SemVer token whose patch component looks like .201 but is a "
                "version, not a host — must stay clean"
            ),
            mutation_of="c-base-localhost",
        ),
        # --- suppression escape hatches -----------------------------------------
        ModelCorpusFixture(
            fixture_id="c-mut-line-suppressed",
            source="The broker runs on 192.168.86.201.  <!-- doc-content-ok approved example -->",  # onex-allow-internal-ip OMN-13568 corpus fixture
            description=(
                "LAN IP on a line carrying the doc-content-ok marker — suppressed, "
                "must stay clean"
            ),
            mutation_of="c-base-localhost",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-omn-clean-no-ticket-shape",
            source="The OMNI_HOME path and OMNINODE platform are referenced here.",
            description=(
                "OMNI / OMNINODE tokens that are NOT the OMN-<digits> ticket shape "
                "— must stay clean"
            ),
            mutation_of="c-base-env-var-path",
        ),
    ],
)
