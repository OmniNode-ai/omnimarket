# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15597: command-substitution-aware tokenization for the OMN-15382 shape guard.

``_invalid_check_value_reason`` tokenized ``check_value`` with
``shlex.split(cmd_str, posix=True)`` (OMN-15430). ``shlex`` is a WORD
SPLITTER, not a shell parser — it has no notion of command substitution, so
the double quotes *inside* a ``$(...)`` are read as the outer string's
quotes. On the OMN-15535 binding check::

    state="$(gh pr view 239 ... --jq '.state + " " + (.mergeCommit.oid // "none")')" && test ...

the jq program's ``" "`` closes the outer ``"`` early, shlex splits INSIDE
the substitution, and the guard judges the jq fragment
``" + (.mergeCommit.oid // none)')"`` as the command name. ``shutil.which``
returns ``None`` and the check hard-REDs as
``INVALID_CHECK_VALUE_NOT_A_COMMAND`` *before it is ever shelled out* — a
false RED on a string ``bash -o pipefail -c`` runs to exit 0.

Corpus census at ``onex_change_control@1e6b75f8`` (32,595 command
check_values in ``contracts/OMN-*.yaml``): **59 checks across 33 contracts**
hard-RED from this tokenizer damage, 0 after the fix.

The fix is ``_split_shell_words``: a single left-to-right scan that keeps a
real nesting discipline, so a ``$(...)``/```...``` region is copied verbatim
into its word and its interior never touches the outer quote state.

``bash -n`` is deliberately NOT the oracle — it exits 0 on
``Recorded product receipt: see PR 123`` too, so parse-validity cannot
discriminate prose, which is this guard's entire purpose. AC3 asserts that
directly.

Test groups map 1:1 onto the ticket's acceptance criteria:
  AC1 — the verbatim pre-amendment OMN-15535 check_value is accepted.
  AC2 — every dev-side false-RED class from the 59-check census is accepted,
        and each one really is parseable by a real ``bash``.
  AC3 — prose still hard-REDs, including under a leading ``!``, and
        ``bash -n`` exit 0 is proven insufficient on its own.
  AC4 — parameterised grammar suite over the constructs the ticket lists.
  AC5 — corpus re-census, committed here so a third party can re-run it from
        repo state instead of from a script quoted in a PR body.

R2 (2026-08-01). The first fix made the guard's verdict platform-independent
by allowlisting 29 shell builtins, and the allowlist returns None on sight —
BEFORE the prose judgement — so seven of those names became a prose-laundering
surface: ``set``/``export``/``declare``/``unset``/``readonly``/``let``/``read``
each run a prose check_value to exit 0 in a real shell, which
``_run_command_check`` then reports as ``status=verified``. The guarding test
shipped alongside it parameterised over PROSE_SAMPLES, none of which begins
with any of the 29 words, so it asserted nothing about the change it named.
Both are fixed here: the seven are handled as no-evidence PREFIXES (skipped
with their operands, like a leading ``VAR=VAL``), and
``TestAllowlistAdmissionRule`` now iterates the ACTUAL frozenset against a
real shell so a future addition that swallows prose fails CI.

R3 (2026-08-01). The prefix-builtin rejection introduced by R2 reused the
pre-existing "no resolvable executable token after leading VAR=VAL
assignments/shell operators" message, which misdescribes its own new inputs:
``read -r a b <<< "$(gh api ...)"`` contains no assignment and no control
operator, yet the reason blamed both. AC2's second clause requires a
rejection of something a real shell runs to name the unsupported construct
EXPLICITLY, so naming an absent one is the same misidentification failure
this ticket closes, moved to the rejection path.
``TestAc2RejectionReasonNamesTheConstructThatIsPresent`` pins it: the reason
now names the prefix builtin that consumed the tokens, and the absence of
the constructs it must NOT blame is re-derived from the tokens rather than
asserted by hand. Behaviour (accept/reject) is unchanged by R3.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _NO_EVIDENCE_BUILTIN_PREFIXES,
    _SHELL_CONTROL_OPERATORS,
    _SHELL_KEYWORD_ALLOWLIST,
    _VAR_ASSIGNMENT_RE,
    EvidenceCollector,
    _invalid_check_value_reason,
    _split_shell_words,
)

# --------------------------------------------------------------------------
# Verbatim corpus strings. Every one below is copied character-for-character
# out of onex_change_control@1e6b75f8; they are the artifact under test, so
# they must not be paraphrased or shortened.
# --------------------------------------------------------------------------

# contracts/OMN-15535.yaml :: dod-omn15535-pr-239-merged-at-4bae73ea, the
# PRE-AMENDMENT value (superseded by OCC#5760/d7b16271 only because this
# guard rejected it). Proven to exit 0 under ``bash -o pipefail -c`` in the
# ticket's reproduction table.
OMN_15535_PRE_AMENDMENT = 'state="$(gh pr view 239 --repo jonahgabriel/steel_onslaught --json state,mergeCommit --jq \'.state + " " + (.mergeCommit.oid // "none")\')" && test "$state" = "MERGED 4bae73ea51a4f3d42d24ccdcaf68d9ab61d1a9a5"'  # onex-allow-test-fixture OMN-15597 reason="verbatim OCC check_value under test; the RED/GREEN proof is invalid if it is altered"

# One verbatim sample per DEV-SIDE JUDGED-TOKEN CLASS observed across the 59
# censused checks. The key is the fragment the pre-fix guard mistook for the
# command name — none of which any author ever wrote as a command.
CENSUS_FALSE_RED_SAMPLES: dict[str, str] = {
    # contracts/OMN-15239.yaml :: dod-omn15239-pr-225-merged-at-23a88d09
    "jq-fragment": 'state="$(gh pr view 225 --repo jonahgabriel/steel_onslaught --json state,mergeCommit --jq \'.state + " " + (.mergeCommit.oid // "none")\')" && test "$state" = "MERGED 23a88d09051565270f76cade8bc7b685cd790f38"',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value"
    # contracts/OMN-14436.yaml :: dod-verify-14436-allowlist-fully-digest-bound
    "api": 'A=$(gh api "repos/OmniNode-ai/onex_change_control/contents/scripts/ci/dod_runner_legacy_allowlist.txt?ref=7266a4f1b2fff2bf5a362a60bb1f099dd94d1835" --jq .content 2>/dev/null | base64 -d 2>/dev/null); T=$(printf \'%s\' "$A" | grep -vcE \'^[[:space:]]*#|^[[:space:]]*$\'); B=$(printf \'%s\' "$A" | grep -cE \'^OMN-[0-9]+ [0-9a-f]{64}$\'); test "$T" -gt 0 && test "$T" = "$B"',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value"
    # contracts/OMN-15192.yaml :: dod-omn-15192-b3-anchored-forcepush-invariant
    "is:pr": 'AS="$(gh api graphql --paginate -f query=\'query($endCursor:String){ search(query:"repo:OmniNode-ai/onex_change_control is:pr label:\\"occ:machine-minted\\" in:title \\"OCC companion for OmniNode-ai\\" created:>2026-07-29T03:01:01Z", type:ISSUE, first:100, after:$endCursor) { pageInfo{hasNextPage endCursor} nodes { ... on PullRequest { number } } } }\' --jq \'.data.search.nodes[].number\' | sort -u | paste -sd, -)" && [ "$AS" = "5466" ]',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value (node selection trimmed only to keep one line readable; the outer nested-quote shape under test is unchanged)"
    # contracts/OMN-15301.yaml :: dod-omn15301-pr-727-file-scope-supersede-15328
    # — the unquoted ``VAR=$(cmd -flag)`` shape (OMN-15476 defect-1 family).
    "-d)": 'd=$(mktemp -d) && gh run download 30347509136 --repo OmniNode-ai/omnibase_infra -n build-manifest-candidate -D "$d" && jq -e \'.prod_pinnable == false\' "$d/build-manifest.json" >/dev/null; rc=$?; rm -rf "$d"; exit $rc',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value (artifact name trimmed; the unquoted $() shape under test is unchanged)"
    # contracts/OMN-15484.yaml :: dod-OmniNode-ai-omnibase-infra-pr-2570-attempt1
    "/": "out=\"$(gh api 'repos/OmniNode-ai/omnibase_infra/actions/runs/30565261108/attempts/1/jobs' --jq '.jobs[]|select(.name==\"merge-hold-gate / evaluate\")|.conclusion')\" && printf '%s\\n' \"$out\" | grep -qx success",  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value"
    # contracts/OMN-15142.yaml :: dod-infra2453-consolidated-env-var-docs-v2 —
    # a subshell-grouped command; pre-fix the word was ``(gh``.
    "(gh": "(gh api 'repos/OmniNode-ai/omnibase_infra/contents/scripts/deploy-runners.sh?ref=81407bcb0ae4f1c1d4215637d01ab9000edc3238' --jq .content | base64 -d | grep -q 'DEPLOY_RUNNER_OMNI_HOME')",  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value"
    # contracts/OMN-9278.yaml :: dod-001 — pre-fix word was ``([``; this was
    # the last survivor of the census and is why ``(`` had to become a
    # word-terminating metacharacter.
    "([": 'state=$(gh pr view 1 --repo OmniNode-ai/omnimarket --json state,baseRefName -q \'[.state, .baseRefName] | @tsv\'); ([ "$(echo "$state" | cut -f1)" = "OPEN" ] || [ "$(echo "$state" | cut -f1)" = "MERGED" ]) && [ "$(echo "$state" | cut -f2)" = "main" ]',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value (PR number generalised)"
    # contracts/OMN-14587.yaml :: dod-verify-2300-baseline-92-entries-28e6780c
    # — shlex did not merely mis-split this one, it raised ValueError ("No
    # closing quotation"), so the guard called a real, bash-parseable string
    # unparseable.
    "shlex-ValueError": 'test "$(gh api "repos/OmniNode-ai/omnibase_infra/contents/scripts/ci/canonical_handler_shape_baseline.py?ref=28e6780c342269da6e9057d04d58c68a6bea8c90" --jq .content | base64 -d | grep -cE \'^\\s+"omnibase_infra\\.nodes\\.\')" -eq 92',  # onex-allow-test-fixture OMN-15597 reason="verbatim censused OCC check_value"
}

# onex_change_control#5762 :: contracts/OMN-15472.yaml
# dod-deploy-source-probe. Routed to this lane (ledger 2026-07-31T22:12:43Z,
# lane C3) as another instance of the false-RED class. Measured here: it is
# NOT one — the assignment is QUOTED with no argument, so even the pre-fix
# shlex tokenizer produced ``f=$(mktemp)``, which matches
# _VAR_ASSIGNMENT_RE and is skipped. The genuine ``mktemp`` instance is the
# UNQUOTED-with-flag ``d=$(mktemp -d)`` form above ("-d)" sample). Pinned
# here as a regression case so it stays accepted either way.
OCC_5762_DEPLOY_SOURCE_PROBE = 'f="$(mktemp)" && gh api \'repos/OmniNode-ai/omnimarket/contents/src/omnimarket/nodes/node_projection_registration/handlers/handler_projection_registration.py?ref=f13ef09c422cbb30fe2e448daf6101e99f0069a8\' --jq .content | base64 -d > "$f" && grep \'DEFAULT_SERVICE_URL = ""\' "$f"'  # onex-allow-test-fixture OMN-15597 reason="verbatim OCC#5762 check_value routed to this lane"

PROSE_SAMPLES = (
    "Recorded product receipt: see PR 123",
    "This was verified manually by the operator",
    "note: do the thing",
    "Verified by inspection of the merged diff",
)

# OMN-15597 R2. Prose that leads with a name the round-1 allowlist admitted on
# sight. Every one of these is run to exit 0 by a real ``bash -o pipefail -c``
# (measured under 3.2.57 and 5.3.3, stdin at /dev/null except ``read``, which
# needs only a line on stdin), so while the name was allowlisted the guard
# returned None, ``_run_command_check`` judged by exit code, and the item
# reported ``status=verified`` — a vacuous GREEN on the DoD evidence runner.
#
# PROSE_SAMPLES above contains none of these words, which is exactly why
# parameterising the "allowlist is not a prose-laundering surface" test over
# PROSE_SAMPLES alone asserted nothing about the allowlist.
BUILTIN_LED_PROSE_SAMPLES: tuple[tuple[str, str], ...] = (
    ("set", "set up the runtime and verified manually"),
    ("export", "export the evidence to the ticket"),
    ("declare", "declare victory"),
    ("unset", "unset the flag manually"),
    ("readonly", "readonly evidence recorded"),
    # Not in the reported five; found by measuring the whole allowlist rather
    # than the reported subset. ``let`` returns 0 whenever the LAST operand is
    # a non-zero arithmetic value, and ``read`` returns 0 whenever stdin has a
    # line to consume — neither is under the check author's control.
    ("let", "let the record show 3"),
    ("read", "read the receipt"),
)

# OMN-15597 R3. check_values whose ONLY command is a no-evidence prefix
# builtin. The guard rejects these deliberately (they prove nothing and are
# shape-indistinguishable from the prose row above), but the REJECTION REASON
# has to name the construct that is actually present. The last two rows are
# the ones R2 got wrong: they contain no ``VAR=VAL`` assignment and no shell
# control operator, yet the R2 message blamed exactly those — misidentifying
# the offending construct on the rejection path is the same failure class
# this ticket exists to close on the acceptance path (AC2, second clause).
PREFIX_BUILTIN_ONLY_SAMPLES: tuple[tuple[str, str], ...] = (
    ("set", "set -euo pipefail"),
    ("export", "export FOO=bar"),
    ("unset", "unset FOO"),
    # No assignment, no operator — a herestring feeding a command
    # substitution. ``bash -n`` rc=0 and ``bash -o pipefail -c`` rc=0.
    ("read", 'read -r a b <<< "$(gh api repos/o/r --jq .name)"'),
    ("read", "read -r x < /dev/null"),
)

_BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _bash_parses(cmd_str: str) -> bool:
    """True when a REAL shell can parse ``cmd_str`` (``bash -n``, no execution)."""
    assert _BASH is not None
    return (
        subprocess.run(
            [_BASH, "-n", "-c", cmd_str],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _bash_exit_status(cmd_str: str, *, stdin_text: str | None = None) -> int:
    """Run ``cmd_str`` the way ``_run_command_check`` does and return its status.

    This is the oracle the guard is measured against: a check_value that a
    real shell runs to exit 0 is a GREEN on the evidence runner, so anything
    prose-shaped that reaches here is a vacuous GREEN.
    """
    assert _BASH is not None
    completed = subprocess.run(
        [_BASH, "-o", "pipefail", "-c", cmd_str],
        capture_output=True,
        text=True,
        timeout=15,
        **(
            {"input": stdin_text}
            if stdin_text is not None
            else {"stdin": subprocess.DEVNULL}
        ),
    )
    return completed.returncode


def _run_single_check(
    tmp_path: Path, check_value: str, ticket_id: str = "OMN-15597"
) -> object:
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "OMN-15597 command-substitution shape guard",
                "checks": [{"check_type": "command", "check_value": check_value}],
            }
        ],
    }
    path = tmp_path / f"{ticket_id}.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    return EvidenceCollector().collect(ticket_id, contract_path=str(path))[0]


# --------------------------------------------------------------------------
# AC1 — RED-before / GREEN-after on the exact string.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAc1Omn15535PreAmendmentStringIsAccepted:
    """Against unmodified ``omnimarket@dev`` these assertions FAIL: the
    pre-fix guard returns ``first token '" + (.mergeCommit.oid // none)\\')"'
    is not a resolvable executable ...``. That is the RED half, and it is a
    property of the guard, not of a mock."""

    def test_guard_accepts_the_verbatim_pre_amendment_check_value(self) -> None:
        assert _invalid_check_value_reason(OMN_15535_PRE_AMENDMENT, cwd=None) is None

    def test_shell_first_command_word_is_test_not_a_jq_fragment(self) -> None:
        """The seam this fix actually changes: the word stream. The
        substitution is ONE word (kept verbatim), so the judged command is
        ``test`` — the assignment, the ``&&`` and the jq program are never
        confused for a command name."""
        words = _split_shell_words(OMN_15535_PRE_AMENDMENT)
        assert words[0].startswith("state=$(gh pr view 239 ")
        assert words[0].endswith(")")
        assert words[1] == "&&"
        assert words[2] == "test"

    def test_end_to_end_collector_does_not_flag_invalid_shape(
        self, tmp_path: Path
    ) -> None:
        """Through the real EvidenceCollector. The check may still FAIL for
        unrelated reasons in a hermetic environment (no network / no ``gh``
        auth); what must not appear is the pre-execution shape rejection."""
        result = _run_single_check(tmp_path, OMN_15535_PRE_AMENDMENT)
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" not in (result.message or "")


# --------------------------------------------------------------------------
# AC2 — no false RED on anything a real shell runs.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAc2CensusedFalseRedClassesAreAccepted:
    @pytest.mark.parametrize(
        "judged_token",
        list(CENSUS_FALSE_RED_SAMPLES),
        ids=list(CENSUS_FALSE_RED_SAMPLES),
    )
    def test_censused_sample_is_no_longer_rejected(self, judged_token: str) -> None:
        value = CENSUS_FALSE_RED_SAMPLES[judged_token]
        reason = _invalid_check_value_reason(value, cwd=None)
        assert reason is None, (
            f"still rejected, and the reason names {judged_token!r} — a "
            f"substitution fragment, not a command the author wrote: {reason}"
        )

    @requires_bash
    @pytest.mark.parametrize(
        "judged_token",
        list(CENSUS_FALSE_RED_SAMPLES),
        ids=list(CENSUS_FALSE_RED_SAMPLES),
    )
    def test_censused_sample_really_is_shell_parseable(self, judged_token: str) -> None:
        """The AC2 oracle, driven for real: each sample is something a shell
        parses and would run to a genuine exit status, so rejecting it was
        always a false RED."""
        assert _bash_parses(CENSUS_FALSE_RED_SAMPLES[judged_token])

    def test_occ_5762_deploy_source_probe_is_accepted(self) -> None:
        assert (
            _invalid_check_value_reason(OCC_5762_DEPLOY_SOURCE_PROBE, cwd=None) is None
        )


# --------------------------------------------------------------------------
# AC3 — prose still hard-REDs (non-regression). This is the guard's purpose;
# widening tokenization must not widen acceptance.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAc3ProseStillRejected:
    @pytest.mark.parametrize("prose", PROSE_SAMPLES)
    def test_prose_is_rejected(self, prose: str) -> None:
        assert _invalid_check_value_reason(prose, cwd=None) is not None

    def test_first_token_ending_in_colon_is_rejected(self) -> None:
        reason = _invalid_check_value_reason("note: do the thing", cwd=None)
        assert reason is not None
        assert "ends with ':'" in reason

    @requires_bash
    @pytest.mark.parametrize("prose", PROSE_SAMPLES)
    def test_bash_dash_n_alone_never_grants_acceptance(self, prose: str) -> None:
        """``bash -n`` exits 0 on all of these. If parse-validity were the
        oracle, every one would be accepted and the guard would be dead —
        which is exactly why the fix produces the shell's first command WORD
        and then applies the prose heuristic to it."""
        assert _bash_parses(prose), "sample no longer exercises the point"
        assert _invalid_check_value_reason(prose, cwd=None) is not None

    def test_prose_under_a_leading_negation_is_still_rejected(self) -> None:
        """``!`` moved out of the keyword allowlist (which accepted the whole
        check_value on sight) into the skipped-modifier set, so the prose
        behind it is still judged."""
        assert _invalid_check_value_reason("! Recorded receipt: see PR 1", cwd=None)

    def test_prose_end_to_end_is_a_failed_check(self, tmp_path: Path) -> None:
        result = _run_single_check(
            tmp_path, "Recorded product receipt: uv run pytest x"
        )
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")


# --------------------------------------------------------------------------
# AC4 — grammar regression suite. Oracle for every row: does a real shell
# parse it and reach a genuine command?
# --------------------------------------------------------------------------

GRAMMAR_CASES: tuple[tuple[str, str, bool], ...] = (
    # (label, check_value, expected_accepted)
    (
        "nested-quote-command-substitution",
        'x="$(gh pr view 1 --jq \'.a + " " + .b\')" && test -n "$x"',
        True,
    ),
    (
        "unquoted-var-command-substitution",
        "body=$(gh api repos/o/r --jq .body) && printf '%s' \"$body\" | grep -q x",
        True,
    ),
    ("nested-dollar-paren", 'x="$(echo "$(date -u +%Y)")" && test -n "$x"', True),
    ("ansi-c-quoting", "printf $'a\\nb\\t' | grep -q a", True),
    (
        "trailing-backslash-line-continuation",
        "gh pr view 1 \\\n  --json state --jq .state",
        True,
    ),
    ("leading-negation", "! test -f /nonexistent/path", True),
    # Restored to the ``cd``-leading form (2d7abf4d had rewritten it to lead
    # with ``test`` so it would pass on Linux). Leading with ``cd`` is the
    # whole point: ``shutil.which("cd")`` resolves on macOS and not on Linux,
    # so this row is what pins the verdict as platform-INDEPENDENT. The
    # rewritten string is kept alongside it rather than dropped.
    ("subshell-group", "(cd /tmp && ls) && echo done", True),
    ("subshell-group-guarded", "(test -d /tmp && cd /tmp && ls) && echo done", True),
    ("backtick-substitution", 'test "`date -u +%Y`" = "2026"', True),
    ("leading-command-substitution", "$(printf ls) /tmp", True),
    ("bracket-test-builtin", "[ -d /tmp ] && echo ok", True),
    ("prose-plain", "Recorded product receipt: see PR 123", False),
    ("prose-sentence", "This was verified manually by the operator", False),
    ("prose-behind-negation", "! Recorded receipt: see PR 1", False),
    ("prose-after-assignment", "X=1 Recorded the receipt", False),
    # bash accepts a trailing backslash (continuation to EOF), so the guard
    # must too — refusing it would be a NEW false RED of this ticket's class.
    ("trailing-backslash-at-eof", "echo hi \\", True),
    ("unterminated-single-quote", "echo 'unbalanced", False),
    ("unterminated-command-substitution", 'x="$(gh pr view 1" && echo hi', False),
    ("assignments-and-operators-only", "FOO=bar &&", False),
    ("empty", "   ", False),
)


@pytest.mark.unit
class TestAc4GrammarSuite:
    @pytest.mark.parametrize(
        ("label", "check_value", "expected_accepted"),
        GRAMMAR_CASES,
        ids=[c[0] for c in GRAMMAR_CASES],
    )
    def test_grammar_construct_verdict(
        self, label: str, check_value: str, expected_accepted: bool
    ) -> None:
        reason = _invalid_check_value_reason(check_value, cwd=None)
        assert (reason is None) is expected_accepted, (
            f"{label}: expected {'accepted' if expected_accepted else 'rejected'}, "
            f"got reason={reason!r}"
        )

    @requires_bash
    @pytest.mark.parametrize(
        ("label", "check_value"),
        [(c[0], c[1]) for c in GRAMMAR_CASES if c[2]],
        ids=[c[0] for c in GRAMMAR_CASES if c[2]],
    )
    def test_accepted_constructs_are_shell_parseable(
        self, label: str, check_value: str
    ) -> None:
        """Nothing is accepted that a real shell cannot parse."""
        assert _bash_parses(check_value), label

    @requires_bash
    @pytest.mark.parametrize(
        ("label", "check_value"),
        [
            (c[0], c[1])
            for c in GRAMMAR_CASES
            if not c[2] and c[0].startswith("unterminated")
        ],
        ids=[
            c[0] for c in GRAMMAR_CASES if not c[2] and c[0].startswith("unterminated")
        ],
    )
    def test_fail_closed_rejections_are_things_bash_also_refuses(
        self, label: str, check_value: str
    ) -> None:
        """Fail-closed parse rejections are not over-reach: bash refuses them too."""
        assert not _bash_parses(check_value), label

    def test_relative_script_with_declared_cwd_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-1 (OMN-15382) non-regression under the new tokenizer."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        sub = tmp_path / "sub"
        sub.mkdir()
        script = sub / "verify.sh"
        script.write_text("#!/bin/sh\necho verified\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-15597",
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "description": "relative script + cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "./verify.sh",
                            "cwd": "${OMNI_HOME}/sub",
                        }
                    ],
                }
            ],
        }
        path = tmp_path / "OMN-15597.yaml"
        path.write_text(yaml.dump(contract), encoding="utf-8")
        result = EvidenceCollector().collect("OMN-15597", contract_path=str(path))[0]
        assert result.status == EnumEvidenceCheckStatus.VERIFIED, result.message

    def test_relative_script_that_does_not_exist_is_still_rejected(
        self, tmp_path: Path
    ) -> None:
        reason = _invalid_check_value_reason("./missing.sh", cwd=str(tmp_path))
        assert reason is not None
        assert "not a resolvable executable relative to cwd" in reason


@pytest.mark.unit
class TestVerdictIsPlatformIndependentForBuiltins:
    """The guard resolves the first word with ``shutil.which``, so anything
    whose acceptance depends on a binary existing on PATH gives a DIFFERENT
    verdict per platform. macOS ships ``/usr/bin/cd``; Linux does not — so
    ``(cd /tmp && ls)`` passed on a developer Mac and hard-REDed on a Linux
    CI runner. 18 checks in the OCC corpus lead with ``cd``.

    These tests pin the builtin verdicts with ``shutil.which`` forced to
    return ``None``, i.e. they hold on the least-equipped host."""

    @pytest.fixture
    def no_path_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "omnimarket.nodes.node_dod_verify.services.evidence_collector.shutil.which",
            lambda _cmd: None,
        )

    @pytest.mark.parametrize(
        "check_value",
        [
            "(cd /tmp && ls) && echo done",
            "cd /tmp && ls",
            # The ``_NO_EVIDENCE_BUILTIN_PREFIXES`` names are skipped WITH
            # their operands (OMN-15597 R2), so the judged command is the one
            # after the control operator — here a builtin, so the row still
            # holds with PATH lookup disabled.
            "export FOO=bar && cd /tmp",
            "set -e; cd /tmp",
            "unset FOO && cd /tmp",
            "read -r x < /dev/null; cd /tmp",
            "eval 'echo hi'",
            "source ./env.sh",
            "trap 'echo bye' EXIT",
            "exec echo hi",
        ],
    )
    def test_builtin_led_commands_accepted_without_path_lookup(
        self, no_path_lookup: None, check_value: str
    ) -> None:
        assert _invalid_check_value_reason(check_value, cwd=None) is None, check_value

    @pytest.mark.parametrize(
        ("builtin", "check_value"),
        PREFIX_BUILTIN_ONLY_SAMPLES,
        ids=[value for _, value in PREFIX_BUILTIN_ONLY_SAMPLES],
    )
    def test_prefix_builtin_alone_is_rejected_as_proving_nothing(
        self, no_path_lookup: None, builtin: str, check_value: str
    ) -> None:
        """A check_value whose ONLY command is a no-evidence prefix builtin is
        rejected, and the reason NAMES THAT BUILTIN rather than misnaming a
        construct that is not present.

        This IS a narrowing relative to round 1, and a deliberate one: the
        guard cannot distinguish ``unset FOO`` from ``unset the flag
        manually`` by shape, and neither proves anything. Corpus exposure is
        0 — see TestAc5CorpusReCensus, which re-derives the whole OCC
        command corpus and asserts no such value exists.
        """
        reason = _invalid_check_value_reason(check_value, cwd=None)
        assert reason is not None, check_value
        assert repr(builtin) in reason, reason

    @pytest.mark.parametrize("prose", PROSE_SAMPLES)
    def test_prose_still_rejected_without_path_lookup(
        self, no_path_lookup: None, prose: str
    ) -> None:
        """The builtin allowlist must not become a prose-laundering surface."""
        assert _invalid_check_value_reason(prose, cwd=None) is not None

    @pytest.mark.parametrize(
        ("builtin", "prose"),
        BUILTIN_LED_PROSE_SAMPLES,
        ids=[name for name, _ in BUILTIN_LED_PROSE_SAMPLES],
    )
    def test_allowlisted_builtin_led_prose_rejected_without_path_lookup(
        self, no_path_lookup: None, builtin: str, prose: str
    ) -> None:
        """The row the previous revision was missing.

        ``test_prose_still_rejected_without_path_lookup`` above parameterises
        over PROSE_SAMPLES, none of which begins with an allowlisted word — so
        it passes identically with and without the allowlist and asserts
        nothing about it. These strings DO begin with one.
        """
        assert _invalid_check_value_reason(prose, cwd=None) is not None, prose


# --------------------------------------------------------------------------
# AC2, second clause (R3) — when the guard rejects something a real shell
# parses and runs, the reason must name the unsupported grammar construct
# EXPLICITLY. R2 rejected ``read -r a b <<< "$(gh api ...)"`` with "no
# resolvable executable token after leading VAR=VAL assignments/shell
# operators" — a string that contains neither an assignment nor a control
# operator. Blaming a construct that is not present is the same
# misidentification defect as judging a jq fragment to be the command name;
# it just lands on the rejection path instead of the acceptance path.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAc2RejectionReasonNamesTheConstructThatIsPresent:
    _OPERATOR_FREE_SAMPLES: tuple[str, ...] = (
        'read -r a b <<< "$(gh api repos/o/r --jq .name)"',
        "read -r x < /dev/null",
        "unset FOO",
    )

    @pytest.mark.parametrize(
        ("builtin", "check_value"),
        PREFIX_BUILTIN_ONLY_SAMPLES,
        ids=[value for _, value in PREFIX_BUILTIN_ONLY_SAMPLES],
    )
    def test_reason_names_the_prefix_builtin_that_consumed_the_tokens(
        self, builtin: str, check_value: str
    ) -> None:
        reason = _invalid_check_value_reason(check_value, cwd=None)
        assert reason is not None, check_value
        assert repr(builtin) in reason, reason

    @pytest.mark.parametrize("check_value", _OPERATOR_FREE_SAMPLES)
    def test_reason_does_not_blame_a_construct_that_is_absent(
        self, check_value: str
    ) -> None:
        """The absence is re-derived from the tokens, not asserted by hand."""
        tokens = _split_shell_words(check_value)
        assert not any(_VAR_ASSIGNMENT_RE.match(t) for t in tokens), tokens
        assert not any(t in _SHELL_CONTROL_OPERATORS for t in tokens), tokens

        reason = _invalid_check_value_reason(check_value, cwd=None)
        assert reason is not None, check_value
        assert "VAR=VAL" not in reason, reason
        assert "shell control operators" not in reason, reason

    @requires_bash
    @pytest.mark.parametrize("check_value", _OPERATOR_FREE_SAMPLES)
    def test_those_rejections_really_are_things_a_real_shell_parses(
        self, check_value: str
    ) -> None:
        """AC2's precondition: the rejection only needs an explicit reason
        because a real shell DOES parse these. Parse-only (``bash -n``) — the
        herestring row would otherwise reach the network."""
        assert _bash_parses(check_value), check_value

    def test_assignment_only_value_still_gets_the_generic_reason(self) -> None:
        """The generic message is not dead code: it is the accurate one when
        the tokens really were consumed by assignments/operators."""
        reason = _invalid_check_value_reason("FOO=bar &&", cwd=None)
        assert reason is not None
        assert "VAR=VAL" in reason, reason


# --------------------------------------------------------------------------
# AC3 (R2) — the allowlist is not a prose-laundering surface. Driven through
# the REAL EvidenceCollector.collect(), not the guard function alone, because
# the defect this closes is only visible end to end: the guard returned None
# and _run_command_check then judged the item by the shell's exit code.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAc3BuiltinLedProseIsNotLaunderedThroughTheAllowlist:
    @pytest.mark.parametrize(
        ("builtin", "prose"),
        BUILTIN_LED_PROSE_SAMPLES,
        ids=[name for name, _ in BUILTIN_LED_PROSE_SAMPLES],
    )
    def test_guard_rejects_builtin_led_prose(self, builtin: str, prose: str) -> None:
        reason = _invalid_check_value_reason(prose, cwd=None)
        assert reason is not None, (
            f"{prose!r} was ACCEPTED: {builtin!r} is admitted on sight, so the "
            "prose reaches the shell and its exit code becomes the verdict"
        )

    @pytest.mark.parametrize(
        ("builtin", "prose"),
        BUILTIN_LED_PROSE_SAMPLES,
        ids=[name for name, _ in BUILTIN_LED_PROSE_SAMPLES],
    )
    def test_end_to_end_builtin_led_prose_is_a_failed_check(
        self, tmp_path: Path, builtin: str, prose: str
    ) -> None:
        """RED-before/GREEN-after through the artifact that runs.

        Against the parent commit this returned
        ``EnumEvidenceCheckStatus.VERIFIED`` with message ``OK (Nms)`` for all
        seven strings.
        """
        result = _run_single_check(tmp_path, prose)
        assert result.status == EnumEvidenceCheckStatus.FAILED, (
            f"{prose!r} reported {result.status} — vacuous GREEN on the DoD "
            "evidence runner"
        )
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")

    @requires_bash
    @pytest.mark.parametrize(
        ("builtin", "prose"),
        BUILTIN_LED_PROSE_SAMPLES,
        ids=[name for name, _ in BUILTIN_LED_PROSE_SAMPLES],
    )
    def test_these_really_would_have_been_vacuous_greens(
        self, builtin: str, prose: str
    ) -> None:
        """Proves the RED above is against exists-but-wrong, not exists-but-harmless.

        Each string exits 0 under the same shell invocation
        ``_run_command_check`` uses, so accepting it is not a cosmetic
        laxity — it is a PASS verdict on prose.
        """
        stdin_text = "a line of stdin\n" if builtin == "read" else None
        assert _bash_exit_status(prose, stdin_text=stdin_text) == 0, prose


@pytest.mark.unit
class TestAllowlistAdmissionRule:
    """The generalising mechanism, not a hand-listed set of strings.

    ``_SHELL_KEYWORD_ALLOWLIST`` short-circuits the prose judgement, so its
    admission rule is: *no prose-shaped invocation of an admitted name may
    exit 0*. This iterates the ACTUAL frozenset against a REAL shell, so a
    future addition that violates the rule fails here instead of shipping a
    false-GREEN path — which is what happened in round 1, where the guarding
    test only ever saw strings that could not reach the allowlist.
    """

    PROSE_TAILS = (
        "the evidence was recorded manually by the operator",
        "up the runtime and verified manually",
        "victory",
        "evidence recorded",
        "the record show 3",
    )

    @requires_bash
    @pytest.mark.parametrize("name", sorted(_SHELL_KEYWORD_ALLOWLIST))
    def test_no_allowlisted_name_launders_prose(self, name: str) -> None:
        for tail in self.PROSE_TAILS:
            prose = f"{name} {tail}"
            if _invalid_check_value_reason(prose, cwd=None) is not None:
                continue  # guard rejected it — nothing reaches the shell
            assert _bash_exit_status(prose) != 0, (
                f"{prose!r} is ACCEPTED by the guard AND exits 0 in a real "
                f"shell: {name!r} must not be in _SHELL_KEYWORD_ALLOWLIST "
                "(move it to _NO_EVIDENCE_BUILTIN_PREFIXES or drop it)"
            )

    def test_the_two_sets_are_disjoint(self) -> None:
        """A prefix builtin must not also be terminal-accepting."""
        assert not (_SHELL_KEYWORD_ALLOWLIST & _NO_EVIDENCE_BUILTIN_PREFIXES)

    def test_every_reported_swallower_is_out_of_the_allowlist(self) -> None:
        for name, _prose in BUILTIN_LED_PROSE_SAMPLES:
            assert name not in _SHELL_KEYWORD_ALLOWLIST, name
            assert name in _NO_EVIDENCE_BUILTIN_PREFIXES, name


@pytest.mark.unit
class TestUnquotedNewlineIsACommandSeparator:
    """An unquoted newline separates commands; it is not blank space.

    Found by CodeRabbit on this PR. The prefix-builtin scan consumes operands
    up to the next CONTROL OPERATOR, so if a newline is lexed as whitespace,
    ``export FOO=bar\\ngh api ...`` has ``gh api ...`` swallowed as operands of
    ``export`` and is rejected for having no resolvable executable — a NEW
    false RED of exactly the class this ticket closes. ``_split_shell_words``
    emits an unquoted newline as ``;``.
    """

    @pytest.mark.parametrize(
        "check_value",
        [
            "export FOO=bar\ngh api repos/o/r --jq .name",
            "set -euo pipefail\ngh api repos/o/r --jq .name",
            "unset FOO\ngh api repos/o/r --jq .name",
            "read -r x < /dev/null\ngh api repos/o/r --jq .name",
            "declare -i n=1\ngh api repos/o/r --jq .name",
            # No prefix builtin involved — a plain multi-line script body.
            "cd /tmp\nls\ngh api repos/o/r",
        ],
    )
    def test_multiline_command_after_a_prefix_builtin_is_accepted(
        self, check_value: str
    ) -> None:
        assert _invalid_check_value_reason(check_value, cwd=None) is None, check_value

    @requires_bash
    @pytest.mark.parametrize(
        "check_value",
        [
            "export FOO=bar\ngh api repos/o/r --jq .name",
            "cd /tmp\nls\ngh api repos/o/r",
        ],
    )
    def test_those_multiline_forms_really_parse_in_a_real_shell(
        self, check_value: str
    ) -> None:
        assert _bash_parses(check_value)

    def test_newline_is_emitted_as_a_control_operator(self) -> None:
        assert _split_shell_words("export FOO=bar\ngh api") == [
            "export",
            "FOO=bar",
            ";",
            "gh",
            "api",
        ]

    def test_backslash_newline_is_still_a_continuation_not_a_separator(self) -> None:
        """The continuation must NOT become a separator — it joins one command."""
        assert _split_shell_words("gh pr view 1 \\\n  --json state") == [
            "gh",
            "pr",
            "view",
            "1",
            "--json",
            "state",
        ]

    @pytest.mark.parametrize(
        "check_value",
        [
            'printf "a\nb" | grep -q a',
            'x="$(printf \'a\nb\')" && test -n "$x"',
        ],
    )
    def test_newlines_inside_quotes_and_substitutions_are_not_separators(
        self, check_value: str
    ) -> None:
        assert _invalid_check_value_reason(check_value, cwd=None) is None, check_value

    def test_multiline_prose_is_still_rejected(self) -> None:
        """The separator fix must not become a new laundering path."""
        assert (
            _invalid_check_value_reason(
                "export the evidence to the ticket\nverified manually by the operator",
                cwd=None,
            )
            is not None
        )


@pytest.mark.unit
class TestColonIsRejectedByTheProseBranch:
    """``:`` was removed from the allowlist; prove that is behaviour-preserving.

    ``: the evidence was recorded`` exits 0 in a real shell, so ``:`` violated
    the admission rule. It was unreachable — ``first.endswith(":")`` fires
    first — but an unreachable prose-swallower only invites a reordering that
    makes it live.
    """

    @pytest.mark.parametrize(
        "check_value",
        [": the evidence was recorded manually", ": && echo hi", ":"],
    )
    def test_colon_led_values_are_still_rejected(self, check_value: str) -> None:
        reason = _invalid_check_value_reason(check_value, cwd=None)
        assert reason is not None, check_value
        assert "ends with ':'" in reason

    @requires_bash
    def test_colon_led_prose_would_otherwise_be_a_vacuous_green(self) -> None:
        assert _bash_exit_status(": the evidence was recorded manually") == 0


# --------------------------------------------------------------------------
# AC5 — corpus re-census, committed rather than quoted from an ephemeral
# script. Skips when no OCC clone is reachable; when one is, it re-derives
# the whole tokenizer-damage class from the corpus itself.
# --------------------------------------------------------------------------


def _occ_root() -> Path | None:
    """Same resolution order as ``EvidenceCollector._resolve_occ_root``."""
    explicit = os.environ.get("ONEX_CC_REPO_PATH", "").strip()
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    omni_home = os.environ.get("OMNI_HOME", "").strip()
    if omni_home and (Path(omni_home) / "onex_change_control").is_dir():
        return Path(omni_home) / "onex_change_control"
    return None


def _prefix_shlex_judged_token(cmd_str: str) -> str | None:
    """Reproduce the PRE-fix judged token: ``shlex`` + the leading-assignment skip.

    This is the damage oracle, and it is deliberately NOT expressed in terms
    of the current guard: post-fix the current tokenizer never emits a
    fragment, so asking it "is this still damaged?" would be a narrowed
    oracle that reaches 0 by construction (the ticket names that as AC5's
    falsifier). Instead the OLD tokenizer selects the class and the CURRENT
    guard is then asked to clear it.
    """
    try:
        tokens = shlex.split(cmd_str, posix=True)
    except ValueError:
        return None
    idx = 0
    while idx < len(tokens) and (
        _VAR_ASSIGNMENT_RE.match(tokens[idx])
        or tokens[idx] in _SHELL_CONTROL_OPERATORS
        or tokens[idx] in {"!", "("}
    ):
        idx += 1
    return tokens[idx] if idx < len(tokens) else None


def _occ_command_check_values(occ: Path) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for path in sorted((occ / "contracts").glob("OMN-*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        # A malformed contract is not this test's subject; skip it rather than
        # let an unrelated YAML defect mask the census result.
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for item in doc.get("dod_evidence") or []:
            if not isinstance(item, dict):
                continue
            for check in item.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                if check.get("check_type") != "command":
                    continue
                value = check.get("check_value")
                if isinstance(value, str) and value.strip():
                    values.append((path.name, value))
    return values


@pytest.mark.unit
class TestAc5CorpusReCensus:
    @pytest.mark.skipif(
        _occ_root() is None,
        reason="no onex_change_control clone reachable (ONEX_CC_REPO_PATH / OMNI_HOME)",
    )
    def test_tokenizer_damage_class_is_empty(self) -> None:
        """0 checks in the tokenizer-damage class, re-derived from the corpus.

        Damage oracle, per the ticket: the PRE-fix ``shlex`` tokenizer judged
        a token containing an unbalanced ``$(`` (i.e. a fragment of a command
        substitution, not something an author wrote as a command name). For
        every such check_value the CURRENT guard must not return an
        INVALID reason naming that fragment.
        """
        occ = _occ_root()
        assert occ is not None
        values = _occ_command_check_values(occ)
        assert values, f"no command check_values found under {occ}/contracts"

        damaged: list[tuple[str, str, str]] = []
        still_red: list[tuple[str, str, str | None]] = []
        for contract, value in values:
            judged = _prefix_shlex_judged_token(value)
            if judged is None:
                continue
            if judged.count("$(") == judged.count(")"):
                continue
            damaged.append((contract, value, judged))
            reason = _invalid_check_value_reason(value, cwd=None)
            if reason is not None and judged in reason:
                still_red.append((contract, value, reason))

        assert not still_red, (
            f"{len(still_red)} of {len(damaged)} tokenizer-damaged check_values "
            f"are still rejected by a fragment-naming reason "
            f"(corpus {occ}, {len(values)} command check_values): "
            f"{still_red[:3]}"
        )

    @pytest.mark.skipif(
        _occ_root() is None,
        reason="no onex_change_control clone reachable (ONEX_CC_REPO_PATH / OMNI_HOME)",
    )
    def test_no_corpus_check_value_leads_with_a_prefix_builtin_alone(self) -> None:
        """The R2 narrowing has zero corpus exposure.

        Rejecting ``unset FOO``-shaped values is only safe if no real contract
        carries one. Measured here rather than asserted.
        """
        occ = _occ_root()
        assert occ is not None
        offenders = [
            (contract, value)
            for contract, value in _occ_command_check_values(occ)
            if (reason := _invalid_check_value_reason(value, cwd=None)) is not None
            and "no resolvable executable token" in reason
        ]
        assert not offenders, offenders[:5]


# --------------------------------------------------------------------------
# Word-scanner unit coverage — the seam the guard consumes.
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestSplitShellWords:
    def test_substitution_is_one_word_kept_verbatim(self) -> None:
        assert _split_shell_words('x="$(a "b" c)" d') == ['x=$(a "b" c)', "d"]

    def test_operators_are_separate_words_without_whitespace(self) -> None:
        assert _split_shell_words("a&&b||c;d|e&f") == [
            "a",
            "&&",
            "b",
            "||",
            "c",
            ";",
            "d",
            "|",
            "e",
            "&",
            "f",
        ]

    def test_parens_terminate_words(self) -> None:
        assert _split_shell_words('([ "$x" = "y" ])') == [
            "(",
            "[",
            "$x",
            "=",
            "y",
            "]",
            ")",
        ]

    def test_quote_removal_matches_posix_semantics(self) -> None:
        assert _split_shell_words("'a b' \"c d\" e\\ f") == ["a b", "c d", "e f"]

    def test_line_continuation_is_removed(self) -> None:
        assert _split_shell_words("a \\\nb") == ["a", "b"]

    def test_ansi_c_escapes_are_decoded(self) -> None:
        assert _split_shell_words("$'a\\nb'") == ["a\nb"]

    @pytest.mark.parametrize(
        "bad",
        ["echo 'x", 'echo "x', "echo $(x", "echo $'x", "echo `x"],
    )
    def test_unparseable_input_raises_value_error(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unterminated"):
            _split_shell_words(bad)

    def test_trailing_backslash_is_a_continuation_not_an_error(self) -> None:
        """bash accepts it; so must this, or the fix introduces a new false RED."""
        assert _split_shell_words("echo hi \\") == ["echo", "hi"]
