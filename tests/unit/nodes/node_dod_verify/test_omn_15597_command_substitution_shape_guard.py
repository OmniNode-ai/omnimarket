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
"""

from __future__ import annotations

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
            "export FOO=bar && echo hi",
            "set -e; echo hi",
            "eval 'echo hi'",
            "source ./env.sh",
            "read -r x < /dev/null",
            "trap 'echo bye' EXIT",
            "unset FOO",
            "exec echo hi",
        ],
    )
    def test_builtin_led_commands_accepted_without_path_lookup(
        self, no_path_lookup: None, check_value: str
    ) -> None:
        assert _invalid_check_value_reason(check_value, cwd=None) is None, check_value

    @pytest.mark.parametrize("prose", PROSE_SAMPLES)
    def test_prose_still_rejected_without_path_lookup(
        self, no_path_lookup: None, prose: str
    ) -> None:
        """The builtin allowlist must not become a prose-laundering surface."""
        assert _invalid_check_value_reason(prose, cwd=None) is not None


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
