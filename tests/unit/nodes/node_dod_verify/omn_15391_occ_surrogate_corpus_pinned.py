# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

r"""OMN-15391 AC1/AC3/AC4 — the surrogate corpus, pinned and committed.

GENERATED DATA, DO NOT HAND-EDIT. Regeneration recipe at the bottom.

AC1 reads "RED proven first **against the live corpus** ... A gate that only
passes on a hand-built fixture is not accepted." A test that reads a live
``onex_change_control`` working clone satisfies "live" and fails "runs at all"
— no hosted runner has that clone, and the corpus moves fast enough that a
live re-run is not re-running the census the ticket names. The corpus is
therefore snapshotted here at a fixed SHA, verbatim. Same construction as
``omn_15597_occ_census_pinned.py``, and for the same reasons.

The strings below are verbatim OCC ``check_value``s — the artifact under test.
Do not paraphrase, shorten, or reformat them.

Provenance — read this before quoting any number out of this module
-------------------------------------------------------------------
repo:      OmniNode-ai/onex_change_control
sha:       e5ad13fc77aceaa5f3d91ad6f1d93d51affc5c8e
committed: 2026-08-27T17:19:52+00:00
contracts under ``contracts/``:            8194
command ``check_value``s across them:      37241

Census at that SHA, by ``classify_check_value``:

* ``probative``                32432
* ``pr_state_surrogate``       4121
* ``foreign_suite_surrogate``  688

1099 contracts carry at least one surrogate check.
347 of them carry **no probative check at all** — those are the
contracts that read fully verified today while nothing in them can go red for
any product reason. That set is ``ZERO_PROBATIVE_CONTRACTS`` below and it is
the ratchet bound AC3 asks for.

Relationship to the ticket's own filed numbers — they are DIFFERENT
measurements and must not be conflated. The ticket measured ONE greppable
command (``uv run pytest tests/test_evidence_admissibility.py``) and found 29
occurrences across 13 contracts on 2026-07-29. That same command is the
``foreign_suite_surrogate`` class here: 688 occurrences at this SHA. The
ticket said plainly that its figure was "the measured floor for **one**
surrogate command" and that "the true corpus size is unknown and is part of
the work" — the ``pr_state_surrogate`` class (4121 occurrences) is the rest of
that work, and it was never in the ticket's own count.

NEGATIVE CONTROL, measured rather than argued (AC4)
---------------------------------------------------
``CONTENT_BOUND_TOTAL`` / ``CONTENT_BOUND_CLASSIFIED_PROBATIVE`` are the count
of check_values carrying the ``?ref=`` content-pin marker — the one generated
shape whose exit status provably depends on the product diff (see
``occ_evidence_stamp.is_product_observing_check_value``) — and how many of
them this predicate leaves PROBATIVE. They are equal at this SHA: 1439 of
1439. Zero false positives on the only shape known to be
product-observing, over the whole live corpus rather than over a fixture.

Regeneration
------------
Re-run the classifier over a fresh ``onex_change_control`` checkout and rewrite
this module wholesale; never hand-patch a number. Moving the SHA is a
deliberate act — the counts below are a shrink-only ratchet bound, so a
regeneration that RAISES ``len(ZERO_PROBATIVE_CONTRACTS)`` is a regression to
investigate, not a number to accept.
"""

from __future__ import annotations

from typing import Final

#: The pinned OCC commit every number in this module was measured at.
PINNED_OCC_SHA: Final[str] = "e5ad13fc77aceaa5f3d91ad6f1d93d51affc5c8e"

#: Contracts scanned, and command ``check_value``s across them, at that SHA.
PINNED_CONTRACT_COUNT: Final[int] = 8194
PINNED_CHECK_VALUE_COUNT: Final[int] = 37241

#: Census by class at that SHA.
PINNED_CLASS_COUNTS: Final[dict[str, int]] = {
    "probative": 32432,
    "pr_state_surrogate": 4121,
    "foreign_suite_surrogate": 688,
}

#: AC4 negative control, measured over the whole corpus: every ``?ref=``
#: content-pinned command stays PROBATIVE.
CONTENT_BOUND_TOTAL: Final[int] = 1439
CONTENT_BOUND_CLASSIFIED_PROBATIVE: Final[int] = 1439

#: Contracts carrying at least one surrogate check at that SHA.
CONTRACTS_WITH_ANY_SURROGATE: Final[int] = 1099

#: Every contract at the pinned SHA whose ``dod_evidence`` contains NOT ONE
#: check whose exit status can depend on the product change. Each of these
#: reads fully verified today. This is the AC3 ratchet bound: shrink-only.
ZERO_PROBATIVE_CONTRACTS: Final[frozenset[str]] = frozenset(
    {
        "OMN-10221",
        "OMN-10426",
        "OMN-10430",
        "OMN-10517",
        "OMN-10852",
        "OMN-10854",
        "OMN-10858",
        "OMN-10878",
        "OMN-11233",
        "OMN-13953",
        "OMN-14009",
        "OMN-14070",
        "OMN-14115",
        "OMN-14126",
        "OMN-14160",
        "OMN-14169",
        "OMN-14241",
        "OMN-14338",
        "OMN-14404",
        "OMN-14420",
        "OMN-14430",
        "OMN-14431",
        "OMN-14624",
        "OMN-14640",
        "OMN-14688",
        "OMN-14737",
        "OMN-14740",
        "OMN-14746",
        "OMN-14754",
        "OMN-14762",
        "OMN-14773",
        "OMN-14777",
        "OMN-14781",
        "OMN-14799",
        "OMN-14801",
        "OMN-14802",
        "OMN-14806",
        "OMN-14808",
        "OMN-14809",
        "OMN-14811",
        "OMN-14813",
        "OMN-14814",
        "OMN-14835",
        "OMN-14854",
        "OMN-14856",
        "OMN-14857",
        "OMN-14858",
        "OMN-14859",
        "OMN-14860",
        "OMN-14865",
        "OMN-14870",
        "OMN-14874",
        "OMN-14875",
        "OMN-14887",
        "OMN-14895",
        "OMN-14897",
        "OMN-14904",
        "OMN-14905",
        "OMN-14906",
        "OMN-14907",
        "OMN-14915",
        "OMN-14917",
        "OMN-14919",
        "OMN-14953",
        "OMN-14962",
        "OMN-14967",
        "OMN-14987",
        "OMN-14988",
        "OMN-14989",
        "OMN-14990",
        "OMN-14993",
        "OMN-14998",
        "OMN-14999",
        "OMN-15013",
        "OMN-15017",
        "OMN-15018",
        "OMN-15019",
        "OMN-15025",
        "OMN-15030",
        "OMN-15046",
        "OMN-15055",
        "OMN-15057",
        "OMN-15058",
        "OMN-15059",
        "OMN-15061",
        "OMN-15062",
        "OMN-15063",
        "OMN-15066",
        "OMN-15068",
        "OMN-15071",
        "OMN-15072",
        "OMN-15095",
        "OMN-15103",
        "OMN-15104",
        "OMN-15114",
        "OMN-15117",
        "OMN-15118",
        "OMN-15122",
        "OMN-15129",
        "OMN-15131",
        "OMN-15134",
        "OMN-15141",
        "OMN-15144",
        "OMN-15151",
        "OMN-15152",
        "OMN-15155",
        "OMN-15158",
        "OMN-15165",
        "OMN-15169",
        "OMN-15190",
        "OMN-15208",
        "OMN-15213",
        "OMN-15218",
        "OMN-15219",
        "OMN-15221",
        "OMN-15222",
        "OMN-15224",
        "OMN-15226",
        "OMN-15229",
        "OMN-15232",
        "OMN-15242",
        "OMN-15243",
        "OMN-15245",
        "OMN-15248",
        "OMN-15249",
        "OMN-15251",
        "OMN-15261",
        "OMN-15263",
        "OMN-15271",
        "OMN-15276",
        "OMN-15277",
        "OMN-15293",
        "OMN-15296",
        "OMN-15306",
        "OMN-15314",
        "OMN-15315",
        "OMN-15323",
        "OMN-15330",
        "OMN-15340",
        "OMN-15348",
        "OMN-15372",
        "OMN-15378",
        "OMN-15417",
        "OMN-15426",
        "OMN-15431",
        "OMN-15456",
        "OMN-15462",
        "OMN-15475",
        "OMN-15494",
        "OMN-15500",
        "OMN-15509",
        "OMN-15521",
        "OMN-15523",
        "OMN-15525",
        "OMN-15532",
        "OMN-15534",
        "OMN-15538",
        "OMN-15541",
        "OMN-15543",
        "OMN-15547",
        "OMN-15550",
        "OMN-15555",
        "OMN-15556",
        "OMN-15560",
        "OMN-15590",
        "OMN-15598",
        "OMN-15600",
        "OMN-15617",
        "OMN-15634",
        "OMN-15638",
        "OMN-15641",
        "OMN-15653",
        "OMN-15659",
        "OMN-15664",
        "OMN-15667",
        "OMN-15681",
        "OMN-15690",
        "OMN-15702",
        "OMN-15704",
        "OMN-15711",
        "OMN-15712",
        "OMN-15732",
        "OMN-15737",
        "OMN-15745",
        "OMN-15755",
        "OMN-15757",
        "OMN-15769",
        "OMN-15774",
        "OMN-15775",
        "OMN-15776",
        "OMN-15778",
        "OMN-15780",
        "OMN-15784",
        "OMN-15786",
        "OMN-15788",
        "OMN-15812",
        "OMN-15814",
        "OMN-15832",
        "OMN-15836",
        "OMN-15843",
        "OMN-15860",
        "OMN-15868",
        "OMN-15877",
        "OMN-15878",
        "OMN-15911",
        "OMN-15916",
        "OMN-15917",
        "OMN-15923",
        "OMN-15929",
        "OMN-15939",
        "OMN-15940",
        "OMN-15953",
        "OMN-15954",
        "OMN-15961",
        "OMN-15968",
        "OMN-15977",
        "OMN-15979",
        "OMN-15980",
        "OMN-15999",
        "OMN-16013",
        "OMN-16017",
        "OMN-16024",
        "OMN-16027",
        "OMN-16028",
        "OMN-16029",
        "OMN-16078",
        "OMN-16085",
        "OMN-16094",
        "OMN-16097",
        "OMN-16099",
        "OMN-16101",
        "OMN-16103",
        "OMN-16105",
        "OMN-16119",
        "OMN-16122",
        "OMN-16124",
        "OMN-16125",
        "OMN-16129",
        "OMN-16133",
        "OMN-16139",
        "OMN-16141",
        "OMN-16143",
        "OMN-16144",
        "OMN-16145",
        "OMN-16150",
        "OMN-16152",
        "OMN-16153",
        "OMN-16158",
        "OMN-16184",
        "OMN-16192",
        "OMN-16207",
        "OMN-16209",
        "OMN-16237",
        "OMN-16243",
        "OMN-16253",
        "OMN-16258",
        "OMN-16264",
        "OMN-16268",
        "OMN-16269",
        "OMN-16271",
        "OMN-16272",
        "OMN-16273",
        "OMN-16277",
        "OMN-16280",
        "OMN-16281",
        "OMN-16282",
        "OMN-16297",
        "OMN-16306",
        "OMN-16307",
        "OMN-16308",
        "OMN-16309",
        "OMN-16310",
        "OMN-16313",
        "OMN-16317",
        "OMN-16337",
        "OMN-16350",
        "OMN-16351",
        "OMN-16355",
        "OMN-16359",
        "OMN-16371",
        "OMN-16375",
        "OMN-16385",
        "OMN-16390",
        "OMN-16405",
        "OMN-16416",
        "OMN-16424",
        "OMN-16427",
        "OMN-16428",
        "OMN-16431",
        "OMN-16433",
        "OMN-16437",
        "OMN-16438",
        "OMN-16441",
        "OMN-16450",
        "OMN-16455",
        "OMN-16457",
        "OMN-16474",
        "OMN-16480",
        "OMN-16490",
        "OMN-16504",
        "OMN-16510",
        "OMN-16511",
        "OMN-16520",
        "OMN-16541",
        "OMN-16542",
        "OMN-16544",
        "OMN-16545",
        "OMN-16547",
        "OMN-16548",
        "OMN-16552",
        "OMN-16556",
        "OMN-16559",
        "OMN-16561",
        "OMN-16563",
        "OMN-16570",
        "OMN-16581",
        "OMN-16582",
        "OMN-16585",
        "OMN-16587",
        "OMN-16592",
        "OMN-16593",
        "OMN-16594",
        "OMN-16604",
        "OMN-16611",
        "OMN-16618",
        "OMN-16620",
        "OMN-16626",
        "OMN-16630",
        "OMN-16631",
        "OMN-16639",
        "OMN-16641",
        "OMN-16658",
        "OMN-16660",
        "OMN-16663",
        "OMN-16667",
        "OMN-16669",
        "OMN-16673",
        "OMN-16685",
        "OMN-16695",
        "OMN-16701",
        "OMN-16702",
        "OMN-16708",
        "OMN-16709",
        "OMN-16757",
        "OMN-9071",
        "OMN-9785",
        "OMN-9896",
    }
)

#: Verbatim ``(evidence item id, check_value, expected class)`` for the
#: contracts the 2026-08-27 validation wave named as green-but-incomplete,
#: plus this ticket's own motivating case (OMN-15376). Extracted at the pinned
#: SHA. These are the concrete RED instances AC1 requires.
NAMED_VALIDATION_WAVE_CONTRACTS: Final[dict[str, tuple[tuple[str, str, str], ...]]] = {
    "OMN-16620": (
        (
            "dod-OmniNode-ai-omniweb-pr-324",
            "gh pr view 324 --repo OmniNode-ai/omniweb --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omniweb-pr-324",
            "gh pr view 324 --repo OmniNode-ai/omniweb --json files",
            "pr_state_surrogate",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "occ-self-bind-pr-7187",
            "gh pr view 7187 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omninode_infra-pr-1015",
            "gh pr view 1015 --repo OmniNode-ai/omninode_infra --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omninode_infra-pr-1015-ci",
            "gh pr view 1015 --repo OmniNode-ai/omninode_infra --json files",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-7210",
            "gh pr view 7210 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
    ),
    "OMN-16667": (
        (
            "dod-OmniNode-ai-omninode_infra-pr-1017",
            "gh pr view 1017 --repo OmniNode-ai/omninode_infra --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omninode_infra-pr-1017-ci",
            "gh pr view 1017 --repo OmniNode-ai/omninode_infra --json files",
            "pr_state_surrogate",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "occ-self-bind-pr-7212",
            "gh pr view 7212 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-7233",
            "gh pr view 7233 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
    ),
    "OMN-15570": (
        (
            "dod-OmniNode-ai-omnibase_infra-pr-2692",
            "gh api repos/OmniNode-ai/omnibase_infra/contents/src/omnibase_infra/nodes/node_gateway_link_health_projection_compute/handlers/handler_gateway_link_health_projection.py?ref=bfa0b093646471667a265e4d884af53857fa2e10 --jq '.content' | base64 -d | grep -c 'def _require_str'",
            "probative",
        ),
        (
            "dod-OmniNode-ai-omnibase_infra-pr-2692-ci",
            "gh pr view 2692 --repo OmniNode-ai/omnibase_infra --json files",
            "pr_state_surrogate",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "occ-self-bind-pr-6215",
            "gh pr view 6215 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-6885",
            "gh pr view 6885 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omninode_infra-pr-963",
            "gh pr view 963 --repo OmniNode-ai/omninode_infra --json files",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omninode_infra-pr-963-manifest",
            "gh api 'repos/OmniNode-ai/omninode_infra/contents/k8s/migrations/application-relation-ownership.yaml?ref=2441a2eb136af3e79b44f769f7060ace45e0160f' --jq '.content' | base64 -d | grep -E 'name: gateway_link_health|name: gateway_link_health_status|schema: omninode_internal' | wc -l | grep -Eq '^[[:space:]]*[3-9][0-9]*$'",
            "probative",
        ),
    ),
    "OMN-16162": (
        (
            "dod-OmniNode-ai-omniclaude-pr-2001",
            "gh api repos/OmniNode-ai/omniclaude/contents/plugins/onex/hooks/lib/node_event_emit_effect_dispatch.py?ref=19e992b5031ef786c48ad994cc6f23786b6e8814 --jq '.content' | base64 -d | grep -c 'def _parse_payload'",
            "probative",
        ),
        (
            "dod-OmniNode-ai-omniclaude-pr-2001",
            "gh api repos/OmniNode-ai/omniclaude/contents/plugins/onex/hooks/lib/node_event_emit_effect_dispatch.py?ref=19e992b5031ef786c48ad994cc6f23786b6e8814 --jq '.content' | base64 -d | grep -c 'def _parse_payload'",
            "probative",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "occ-self-bind-pr-6903",
            "gh pr view 6903 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-6918",
            "gh pr view 6918 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-6928",
            "gh pr view 6928 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-6931",
            "gh pr view 6931 --repo OmniNode-ai/onex_change_control --json number,state,headRefName",
            "pr_state_surrogate",
        ),
    ),
    "OMN-15797": (
        (
            "dod-OmniNode-ai-omnimarket-pr-2155",
            "gh api repos/OmniNode-ai/omnimarket/contents/scripts/ci/check_rls_read_tenant_seam.py?ref=a7204d1df56b2dbf799fd8bbfd390af667f32339 --jq '.content' | base64 -d | grep -c 'class Finding'",
            "probative",
        ),
        (
            "dod-OmniNode-ai-omnimarket-pr-2155",
            "gh api repos/OmniNode-ai/omnimarket/contents/scripts/ci/check_rls_read_tenant_seam.py?ref=a7204d1df56b2dbf799fd8bbfd390af667f32339 --jq '.content' | base64 -d | grep -c 'class Finding'",
            "probative",
        ),
        (
            "dod-deploy-assessment",
            "gh api repos/OmniNode-ai/omnimarket/contents/scripts/ci/check_rls_read_tenant_seam.py?ref=a7204d1df56b2dbf799fd8bbfd390af667f32339 --jq '.content' | base64 -d | grep -c 'class Finding'",
            "probative",
        ),
        (
            "dod-deploy-assessment-sup",
            "gh api repos/OmniNode-ai/omnimarket/contents/scripts/ci/check_rls_read_tenant_seam.py?ref=a7204d1df56b2dbf799fd8bbfd390af667f32339 --jq '.content' | base64 -d | grep -c 'class Finding'",
            "probative",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "occ-self-bind-pr-7236",
            "gh pr view 7236 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omnimarket-pr-2158",
            "gh api repos/OmniNode-ai/omnimarket/contents/tests/unit/projection/test_omn15797_serving_tenant_context.py?ref=cafe32a5ab7c91442b6aedfa653389ec44f7bdc3 --jq '.content' | base64 -d | grep -c 'def test_refusal_does_not_echo_internal_exception_text'",
            "probative",
        ),
        (
            "dod-OmniNode-ai-omnimarket-pr-2158-ci",
            "gh pr view 2158 --repo OmniNode-ai/omnimarket --json files",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-7246",
            "gh pr view 7246 --repo OmniNode-ai/onex_change_control --json number,state",
            "pr_state_surrogate",
        ),
    ),
    "OMN-15376": (
        (
            "dod-OmniNode-ai-omnibase_infra-pr-2537",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omnibase_infra-pr-2537",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json files",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omnimarket-pr-1946",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-OmniNode-ai-omnimarket-pr-1946",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json files",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-5421",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-5425",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "pr_state_surrogate",
        ),
        (
            "occ-self-bind-pr-5424",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "pr_state_surrogate",
        ),
        (
            "dod-omn-15376-execution-proof",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "dod-omn-15376-static-gate",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "dod-omn-15376-vendor-parity",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "dod-occ-evidence-admissibility-validator",
            "uv run pytest tests/test_evidence_admissibility.py -q",
            "foreign_suite_surrogate",
        ),
        (
            "dod-omn-15376-ac2-reconciling-alter",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=78b8731103a019ea90eee3164b53e882a064fe34' | grep -q 'ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS aggregation_key'",
            "probative",
        ),
        (
            "dod-omn-15376-ac2-reconciling-alter",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=11b460db23d40dd2ca7704d0c3e9fd89851af60c' | grep -c 'ADD COLUMN IF NOT EXISTS aggregation_key' | grep -qx 0",
            "probative",
        ),
        (
            "dod-omn-15376-ac2-fix-live-on-mainline",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=dev' | grep -q 'ALTER TABLE llm_cost_aggregates ADD COLUMN IF NOT EXISTS aggregation_key'",
            "probative",
        ),
        (
            "dod-omn-15376-ac2-fix-live-on-mainline",
            "gh api repos/OmniNode-ai/omnibase_infra/compare/dev...78b8731103a019ea90eee3164b53e882a064fe34 --jq '.ahead_by' | grep -qx 0",
            "probative",
        ),
        (
            "dod-omn-15376-execution-proof-landed",
            "gh api repos/OmniNode-ai/omnibase_infra/commits/78b8731103a019ea90eee3164b53e882a064fe34 --jq '.files[].filename' | grep -qx 'tests/integration/migrations/test_node_migration_shape_drift_omn15376.py'",
            "probative",
        ),
        (
            "dod-omn-15376-static-gate-landed",
            "gh api repos/OmniNode-ai/omnibase_infra/commits/78b8731103a019ea90eee3164b53e882a064fe34 --jq '.files[].filename' | grep -qx 'tests/ci/test_node_migration_shape_reconciliation.py'",
            "probative",
        ),
        (
            "dod-omn-15376-vendor-parity-digest",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnimarket/contents/src/omnimarket/nodes/node_projection_cost_summary/migrations/0001_create_llm_cost_aggregates.sql?ref=708dadaa966bbbb584e806872f1c088027285743' | shasum -a 256 | grep -q 60cad6737c615c1cd6275d5818f0b45e9d8e57bbf1d2f265b1ebba3f6dc2ea4d",
            "probative",
        ),
        (
            "dod-omn-15376-vendor-parity-digest",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=78b8731103a019ea90eee3164b53e882a064fe34' | shasum -a 256 | grep -q 60cad6737c615c1cd6275d5818f0b45e9d8e57bbf1d2f265b1ebba3f6dc2ea4d",
            "probative",
        ),
        (
            "dod-omn-15376-pr-2537-merged-commit",
            "gh api repos/OmniNode-ai/omnibase_infra/commits/78b8731103a019ea90eee3164b53e882a064fe34 --jq '.commit.message' | grep -q '(#2537)'",
            "probative",
        ),
        (
            "dod-omn-15376-pr-1946-merged-commit",
            "gh api repos/OmniNode-ai/omnimarket/commits/708dadaa966bbbb584e806872f1c088027285743 --jq '.commit.message' | grep -q '(#1946)'",
            "probative",
        ),
        (
            "dod-omn-15376-occ-5421-merged-commit",
            "gh api repos/OmniNode-ai/onex_change_control/commits/2284761dedc18b26f91d8d1d09e2ae41255226bb --jq '.commit.message' | grep -q '(#5421)'",
            "probative",
        ),
        (
            "dod-omn-15376-occ-5424-merged-commit",
            "gh api repos/OmniNode-ai/onex_change_control/commits/9fca83aeb9c17055a3504207cbc24d6075872af8 --jq '.commit.message' | grep -q '(#5424)'",
            "probative",
        ),
        (
            "dod-omn-15376-occ-5425-merged-commit",
            "gh api repos/OmniNode-ai/onex_change_control/commits/7d04f3424cc789b42269ef59f31a0d5aed34d8ee --jq '.commit.message' | grep -q '(#5425)'",
            "probative",
        ),
        (
            "dod-15376-infra2537-baselines-rb15391",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_baselines/0001_create_baselines_tables.sql?ref=78b8731103a019ea90eee3164b53e882a064fe34' | grep -q 'ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS snapshot_id'",
            "probative",
        ),
        (
            "dod-15376-infra2537-baselines-rb15391",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_baselines/0001_create_baselines_tables.sql?ref=11b460db23d40dd2ca7704d0c3e9fd89851af60c' | grep -c 'ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS snapshot_id' | grep -qx 0",
            "probative",
        ),
        (
            "dod-15376-infra2537-baselines-rb2",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_baselines/0001_create_baselines_tables.sql?ref=78b8731103a019ea90eee3164b53e882a064fe34' | grep -q 'ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS snapshot_id'",
            "probative",
        ),
        (
            "dod-15376-infra2537-baselines-rb2",
            "body=$(gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_baselines/0001_create_baselines_tables.sql?ref=11b460db23d40dd2ca7704d0c3e9fd89851af60c') && printf '%s' \"$body\" | grep -qF 'OMN-11887: node-owned projection migration' && ! printf '%s' \"$body\" | grep -qF 'ALTER TABLE baselines_snapshots ADD COLUMN IF NOT EXISTS snapshot_id'",
            "probative",
        ),
        (
            "dod-omn-15376-ac2-reconciling-rb2",
            "gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=78b8731103a019ea90eee3164b53e882a064fe34' | grep -q 'ADD COLUMN IF NOT EXISTS aggregation_key'",
            "probative",
        ),
        (
            "dod-omn-15376-ac2-reconciling-rb2",
            "body=$(gh api -H 'Accept: application/vnd.github.raw' 'repos/OmniNode-ai/omnibase_infra/contents/docker/migrations/forward/nodes/node_projection_cost_summary/0001_create_llm_cost_aggregates.sql?ref=11b460db23d40dd2ca7704d0c3e9fd89851af60c') && printf '%s' \"$body\" | grep -qF 'node-owned projection migration' && ! printf '%s' \"$body\" | grep -qF 'ADD COLUMN IF NOT EXISTS aggregation_key'",
            "probative",
        ),
    ),
}

__all__: list[str] = [
    "CONTENT_BOUND_CLASSIFIED_PROBATIVE",
    "CONTENT_BOUND_TOTAL",
    "CONTRACTS_WITH_ANY_SURROGATE",
    "NAMED_VALIDATION_WAVE_CONTRACTS",
    "PINNED_CHECK_VALUE_COUNT",
    "PINNED_CLASS_COUNTS",
    "PINNED_CONTRACT_COUNT",
    "PINNED_OCC_SHA",
    "ZERO_PROBATIVE_CONTRACTS",
]
