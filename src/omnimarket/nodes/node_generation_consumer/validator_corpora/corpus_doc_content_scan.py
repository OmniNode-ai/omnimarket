# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: OMN-13568 — this corpus's fixtures ARE the doc-sanitization
# violations (LAN IPs, .201 host shorthand, personal paths, ssh/email, OMN refs)
# the generated doc_content_scan scanner-under-test must flag; the literals are the subject.
# onex-allow-internal-ip OMN-13568 reason="corpus fixtures are intentional local-env doc leaks the scanner-under-test must flag"
# onex-allow-file OMN-13568 reason="this acceptance corpus's entire subject is doc-sanitization leak literals (LAN IP, host shorthand, personal path, ssh/email, OMN ref) the generated scanner must flag; per-line markers would obscure the fixtures"
# onex-allow-file-internal-ip OMN-13568 reason="this acceptance corpus's entire subject is local-env doc leak literals the generated scanner must flag; per-line markers would obscure the fixtures"
"""Acceptance corpus for the doc-content-scan mechanical scanner (OMN-13568, doc-sanitization chain).

Ground truth: PUBLIC-repo documentation must not leak local-environment traces
(RFC1918 LAN IPs, ``.201``/``.200`` host shorthand, personal home paths,
ssh/email addresses) or Linear ticket references (``OMN-XXXX``) in prose. The
sanitization sweep (OMN-13567) removes ~331 local-env leaks + ~3,812 ticket refs
across 14 public repos; this corpus turns that invariant into the acceptance
authority for a generated COMPUTE validator that BLOCKS the regression.

The corpus is the acceptance authority — NOT the LLM. A generated doc-content
scanner is accepted iff it flags every ``violation_fixtures`` entry (>=1 finding)
and produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

What MUST be flagged (violation fixtures):

  * RFC1918 LAN IP in doc prose (``192.168.``, ``10.``, ``172.16``-``172.31``).
  * ``.201`` / ``.200`` host shorthand referring to a host
    ("deployed to .201", "ssh ...@.201").
  * Personal home path (``/Users/<user>/...``, ``/home/<user>/...``).
  * ssh / email leak (``ssh jonah@192.168.86.201``, ``<name>@gmail.com``).
  * ``OMN-XXXX`` in doc prose / parenthetical / heading / list item / link target.

What MUST stay clean (clean fixtures):

  * ``localhost`` / ``127.0.0.1`` — LEFT by decision (legit dev examples).
  * Doc-reserved IP ranges (``192.0.2.``, ``198.51.100.``, ``203.0.113.``) and
    ``example.com``.
  * Portable / env-var forms (``$OMNI_HOME``, ``${ONEX_HOST}``, ``Path.home()``).
  * Suppressed lines (``# doc-content-ok``) / suppressed files (``# doc-content-file-ok``).
  * SemVer / decimal tokens shaped like ``.200`` but NOT a host
    shorthand (e.g. ``0.200``, version ``2.201.0``).

Mutation cases (``mutation_of``) are adversarial perturbations of a base fixture:
a different RFC1918 band, a Markdown-heading vs list-item placement of the same
leak, a single- vs double-host shorthand, and a near-miss decimal that must NOT
fire. They prove the scanner generalises the invariant rather than memorising a
curated set (OMN-13289 DoD: >=1 mutation case or the corpus is rejected).
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
        # --- base cases: one per leak family, the canonical shape ---
        ModelCorpusFixture(
            fixture_id="v-base-lan-ip",
            source="Deploy the broker to the host at 192.168.86.201 and verify.",  # onex-allow-internal-ip OMN-13568 corpus fixture: the leak the scanner must flag
            description="RFC1918 192.168/16 LAN IP in doc prose — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-host-shorthand",
            source="The runtime is deployed to .201 in the stability lane.",
            description="`.201` host shorthand referring to a host — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-personal-path",
            source="Clone the repo into /Users/jonah/Code/omni_home to begin.",
            description="personal home path /Users/<user>/... in prose — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-ssh-email",
            source="Connect with `ssh jonah@192.168.86.201` then run the smoke test.",  # onex-allow-internal-ip OMN-13568 corpus fixture: the leak the scanner must flag
            description="ssh user@LAN-IP leak in a doc command — must flag",
        ),
        ModelCorpusFixture(
            fixture_id="v-base-omn-ref",
            source="The dispatch gate was hardened in OMN-9731 after two bypasses.",
            description="OMN-XXXX ticket ref in doc prose — must flag",
        ),
        # --- adversarial mutation cases (must still flag) ---
        ModelCorpusFixture(
            fixture_id="v-mut-lan-ip-10-band",
            source="The judge lane resolves its broker at 10.0.0.42 internally.",  # onex-allow-internal-ip OMN-13568 corpus fixture: the leak the scanner must flag
            description="mutated to the 10/8 RFC1918 band — must still flag",
            mutation_of="v-base-lan-ip",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-lan-ip-172-band",
            source="Point the projection reader at 172.20.5.10 for the read replica.",  # onex-allow-internal-ip OMN-13568 corpus fixture: the leak the scanner must flag
            description="mutated to the 172.16-31/12 RFC1918 band — must still flag",
            mutation_of="v-base-lan-ip",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-host-shorthand-200",
            source="The 284B model serves from .200 (the Mac Studio).",
            description="mutated `.200` host shorthand (sibling host) — must still flag",
            mutation_of="v-base-host-shorthand",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-personal-path-home",
            source="Logs land under /home/jonah/.onex/state on the runner.",
            description="mutated to /home/<user>/... personal path form — must still flag",
            mutation_of="v-base-personal-path",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-email-leak",
            source="Escalate to the maintainer at jonah@gmail.com for grants.",
            description="mutated to a bare personal email leak — must still flag",
            mutation_of="v-base-ssh-email",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-ref-heading",
            source="## Migration ledger (OMN-13206)\n\nExact consumer lists below.",
            description="mutated OMN-XXXX ref into a Markdown heading parenthetical — must still flag",
            mutation_of="v-base-omn-ref",
        ),
        ModelCorpusFixture(
            fixture_id="v-mut-omn-ref-list-item",
            source="- See OMN-12525 for the canonical three-primitive architecture.",
            description="mutated OMN-XXXX ref into a Markdown list item — must still flag",
            mutation_of="v-base-omn-ref",
        ),
    ],
    clean_fixtures=[
        # --- localhost / loopback are LEFT by decision ---
        ModelCorpusFixture(
            fixture_id="c-base-localhost",
            source="Run the dev server and open http://localhost:3000 in a browser.",
            description="localhost dev example (LEFT by decision) — must stay clean",
        ),
        ModelCorpusFixture(
            fixture_id="c-base-loopback",
            source="The probe binds to 127.0.0.1 for local-only health checks.",
            description="127.0.0.1 loopback (LEFT by decision) — must stay clean",
        ),
        # --- doc-reserved IP ranges + example.com ---
        ModelCorpusFixture(
            fixture_id="c-base-doc-reserved-ip",
            source="Example config: set HOST to 192.0.2.10 (a TEST-NET-1 placeholder).",
            description="192.0.2.0/24 doc-reserved (RFC5737) placeholder IP — must stay clean",
        ),
        ModelCorpusFixture(
            fixture_id="c-base-example-domain",
            source="Send webhooks to https://hooks.example.com/ingest in your config.",
            description="example.com reserved doc domain — must stay clean",
        ),
        # --- portable / env-var forms (the correct alternative to a leak) ---
        ModelCorpusFixture(
            fixture_id="c-base-env-var-path",
            source="Clone the repo into $OMNI_HOME to begin.",
            description="$OMNI_HOME portable env-var path (not a personal path) — must stay clean",
        ),
        # --- adversarial clean mutations (near-misses that must NOT fire) ---
        ModelCorpusFixture(
            fixture_id="c-mut-doc-reserved-203",
            source="Example config: point the client at 203.0.113.7 in the sample.",
            description=(
                "203.0.113.0/24 doc-reserved (RFC5737) — a near-miss for a public/LAN "
                "IP that must stay clean"
            ),
            mutation_of="c-base-doc-reserved-ip",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-semver-decimal",
            source="The legacy runtime shipped as version 0.200 before the rewrite.",
            description=(
                "`0.200` is a decimal SemVer token, NOT a `.200` host shorthand — "
                "must stay clean"
            ),
            mutation_of="c-base-loopback",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-env-var-braced",
            source="Resolve the broker from ${ONEX_HOST} at the effect boundary.",
            description=(
                "${ONEX_HOST} braced env-var form (portable, not a leak) — must stay clean"
            ),
            mutation_of="c-base-env-var-path",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-path-home-callable",
            source="Resolve the state dir via Path.home() / '.onex' in code.",
            description=(
                "Path.home() portable home resolution (not a personal /Users path) — "
                "must stay clean"
            ),
            mutation_of="c-base-env-var-path",
        ),
        # --- suppression escape hatches ---
        ModelCorpusFixture(
            fixture_id="c-mut-line-suppressed",
            source="Deployed to 192.168.86.201 for the demo.  <!-- doc-content-ok -->",  # onex-allow-internal-ip OMN-13568 corpus fixture: the suppressed-line leak literal
            description=(
                "LAN IP on a line carrying the `# doc-content-ok` suppression marker — "
                "suppressed, must stay clean"
            ),
            mutation_of="c-base-doc-reserved-ip",
        ),
        ModelCorpusFixture(
            fixture_id="c-mut-file-suppressed",
            source="<!-- doc-content-file-ok -->\nHistorical trace: ssh jonah@.201; see OMN-1234.",
            description=(
                "file carrying the `# doc-content-file-ok` whole-file suppression marker "
                "on its first line — every leak below is suppressed, must stay clean"
            ),
            mutation_of="c-base-doc-reserved-ip",
        ),
    ],
)
