# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure-seam tests for the OMN-15247 contention + content-probe modules.

Every test here fails against pre-OMN-15247 ``dev`` for the strongest available
reason: the modules under test (``omnimarket.occ_contention``,
``omnimarket.occ_content_probe``) do not exist there, and ``resolve_red_ref`` /
``is_shell_safe_check`` have no predecessor anywhere in the repo. The RED-vs-
EXISTS-but-WRONG distinction that matters is exercised in
``test_occ_autobind_contention_omn_15247.py``, which drives the REAL emitter and
asserts on the CONTRACT's declared check_value — the artifact the OCC
contract-compliance runner actually executes.

Wired into the blocking ``occ-emitter-golden-gate.yml`` suite list.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import (
    MAX_CHECK_VALUE_LENGTH,
    build_content_read_check,
    is_shell_safe_check,
    resolve_red_ref,
)
from omnimarket.occ_contention import (
    CHECK_BINDING_ENV_VAR,
    ContentionFinding,
    EnumCheckBinding,
    EnumCompanionProvenance,
    classify_companion_provenance,
    companion_touches_ticket,
    decide_contention,
    find_open_companions,
    resolve_occ_producer_policy,
)

_MERGE_SHA = "6e91834b7464ab62ac49da3986e2365e45850fd3"
_PARENT_SHA = "f7fb7cdeba293003bfcb2e5eb92d8ac8acc1665b"
_HEAD_SHA = "1d604ccb5aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_BASE_TIP_SHA = "0" * 40
_MERGE_BASE_SHA = "a" * 40


def _finding(
    provenance: EnumCompanionProvenance,
    *,
    ticket: str = "OMN-15232",
    number: int = 5115,
    ref: str = "jonah/omn-15232-occ",
) -> ContentionFinding:
    return ContentionFinding(
        ticket_id=ticket,
        occ_pr_number=number,
        occ_head_ref=ref,
        provenance=provenance,
        reason="fixture",
    )


# ---------------------------------------------------------------------------
# Policy resolution — fail-closed, defaults reproduce today's behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyResolution:
    def test_empty_env_resolves_to_the_shipped_default(self) -> None:
        policy = resolve_occ_producer_policy({})
        assert policy.check_binding is EnumCheckBinding.PR_EXISTENCE

    def test_defer_on_contention_has_no_env_var_to_resolve(self) -> None:
        """The guard is unconditional — no policy field exists to be turned off.

        This is the structural half of the OMN-15247 fix: the earlier slice's
        ``OMNI_OCC_CONTENTION_POLICY=observe`` default reproduced the defect,
        because the production producer reads no env. Asserting the field is
        GONE (not merely re-defaulted) is what stops it coming back.
        """
        policy = resolve_occ_producer_policy({"OMNI_OCC_CONTENTION_POLICY": "observe"})
        assert not hasattr(policy, "contention_policy")
        # An ignored legacy value must not silently re-enable minting either.
        assert policy.check_binding is EnumCheckBinding.PR_EXISTENCE

    @pytest.mark.parametrize(
        ("env", "expected_binding"),
        [
            ({CHECK_BINDING_ENV_VAR: "content_bound"}, EnumCheckBinding.CONTENT_BOUND),
            (
                {CHECK_BINDING_ENV_VAR: "  PR_EXISTENCE  "},
                EnumCheckBinding.PR_EXISTENCE,
            ),
            ({}, EnumCheckBinding.PR_EXISTENCE),
        ],
    )
    def test_recognized_values_resolve(
        self,
        env: dict[str, str],
        expected_binding: EnumCheckBinding,
    ) -> None:
        policy = resolve_occ_producer_policy(env)
        assert policy.check_binding is expected_binding

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            (CHECK_BINDING_ENV_VAR, "1"),
            (CHECK_BINDING_ENV_VAR, "content-bound"),
        ],
    )
    def test_unrecognized_value_raises_naming_var_and_accepted_set(
        self, var: str, value: str
    ) -> None:
        """CLAUDE.md rule 8: fail fast, never silently pick a default on a typo."""
        with pytest.raises(RuntimeError) as excinfo:
            resolve_occ_producer_policy({var: value})
        message = str(excinfo.value)
        assert var in message
        assert value in message
        for accepted in ("pr_existence", "content_bound"):
            assert accepted in message


# ---------------------------------------------------------------------------
# Provenance classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClassifyCompanionProvenance:
    def test_machine_minted_label_wins_regardless_of_branch(self) -> None:
        assert (
            classify_companion_provenance(
                labels=["occ:machine-minted"], head_ref="jonah/hand-authored"
            )
            is EnumCompanionProvenance.MACHINE
        )

    def test_unlabelled_autobind_branch_is_machine(self) -> None:
        """``_apply_machine_minted_label`` is best-effort and swallows failures.

        Without the branch leg, a machine companion whose label call was
        swallowed would classify HAND_AUTHORED and the emitter would defer to
        ITSELF — the highest-severity self-inflicted failure mode here.
        """
        assert (
            classify_companion_provenance(
                labels=[], head_ref="auto/omninode-ai-omnibase_infra-pr-1-occ-autobind"
            )
            is EnumCompanionProvenance.MACHINE
        )

    def test_human_branch_without_label_is_hand_authored(self) -> None:
        assert (
            classify_companion_provenance(labels=[], head_ref="jonah/omn-15232-occ")
            is EnumCompanionProvenance.HAND_AUTHORED
        )

    def test_no_signal_at_all_is_unknown(self) -> None:
        assert (
            classify_companion_provenance(labels=[], head_ref="")
            is EnumCompanionProvenance.UNKNOWN
        )

    def test_partial_autobind_prefix_is_not_machine(self) -> None:
        """``fullmatch``, not a prefix test — ``auto/foo`` alone proves nothing."""
        assert (
            classify_companion_provenance(labels=[], head_ref="auto/something-else")
            is EnumCompanionProvenance.HAND_AUTHORED
        )


# ---------------------------------------------------------------------------
# The falsifiable contention predicate — files, never title text
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCompanionTouchesTicket:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("contracts/OMN-15232.yaml", True),
            ("drift/dod_receipts/OMN-15232/x/command.yaml", True),
            ("contracts/OMN-15233.yaml", False),
            ("drift/dod_receipts/OMN-15233/x/command.yaml", False),
            # The onex_change_control#5129 shape: one net-new narrative doc that
            # NAMES the ticket but declares no dod_evidence. Not contention.
            ("docs/evidence/OMN-15232/note.md", False),
            ("contracts/OMN-152321.yaml", False),
            ("", False),
        ],
    )
    def test_predicate(self, path: str, expected: bool) -> None:
        assert (
            companion_touches_ticket(changed_paths=[path], ticket_id="OMN-15232")
            is expected
        )


# ---------------------------------------------------------------------------
# The decision rule and its deliberate asymmetry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecideContention:
    def test_defers_on_a_hand_authored_contender(self) -> None:
        should_defer, reason = decide_contention(
            [_finding(EnumCompanionProvenance.HAND_AUTHORED)]
        )
        assert should_defer is True
        assert "OCC#5115" in reason

    def test_defers_on_unknown_provenance(self) -> None:
        """Asymmetric on purpose: a needless defer is recoverable, a wrong mint is not."""
        should_defer, _reason = decide_contention(
            [_finding(EnumCompanionProvenance.UNKNOWN, ref="")]
        )
        assert should_defer is True

    def test_does_not_defer_to_a_machine_contender(self) -> None:
        """Machine-vs-machine is the lease's axis (OMN-14793), not this one."""
        should_defer, _reason = decide_contention(
            [_finding(EnumCompanionProvenance.MACHINE, ref="auto/x-occ-autobind")]
        )
        assert should_defer is False

    def test_no_findings_never_defers(self) -> None:
        assert decide_contention([])[0] is False

    def test_the_rule_takes_no_policy_argument(self) -> None:
        """No caller can pass an observe mode back in — the parameter is gone."""
        with pytest.raises(TypeError):
            decide_contention(  # type: ignore[call-arg]
                [_finding(EnumCompanionProvenance.HAND_AUTHORED)], "observe"
            )


# ---------------------------------------------------------------------------
# find_open_companions — self-defer guard + degrade-to-UNKNOWN
# ---------------------------------------------------------------------------


def _search_payload(*numbers: int) -> dict[str, object]:
    return {"items": [{"number": n} for n in numbers]}


@pytest.mark.unit
class TestFindOpenCompanions:
    def test_confirms_by_files_not_by_title_mention(self) -> None:
        findings = find_open_companions(
            tickets=["OMN-15232"],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch="auto/mine-occ-autobind",
            search_issues=lambda _p: _search_payload(5115, 5129),
            get_pull=lambda n: {
                "head": {"ref": "jonah/omn-15232-occ" if n == 5115 else "jonah/doc"},
                "labels": [],
            },
            list_pr_files=lambda n: (
                [{"filename": "contracts/OMN-15232.yaml"}]
                if n == 5115
                else [{"filename": "docs/evidence/OMN-15232/note.md"}]
            ),
        )
        assert [f.occ_pr_number for f in findings] == [5115]
        assert findings[0].provenance is EnumCompanionProvenance.HAND_AUTHORED

    def test_skips_its_own_in_flight_branch(self) -> None:
        """A ``synchronize`` re-fire must never defer to the branch it force-pushes."""
        own = "auto/omninode-ai-omnimarket-pr-321-occ-autobind"
        findings = find_open_companions(
            tickets=["OMN-9999"],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch=own,
            search_issues=lambda _p: _search_payload(4242),
            get_pull=lambda _n: {"head": {"ref": own}, "labels": []},
            list_pr_files=lambda _n: [{"filename": "contracts/OMN-9999.yaml"}],
        )
        assert findings == ()

    def test_search_failure_degrades_to_unknown_with_the_reason_recorded(self) -> None:
        def _boom(_path: str) -> dict[str, object]:
            raise RuntimeError("search API 503")

        findings = find_open_companions(
            tickets=["OMN-15232"],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch="auto/mine-occ-autobind",
            search_issues=_boom,
            get_pull=lambda _n: {},
            list_pr_files=lambda _n: [],
        )
        assert len(findings) == 1
        assert findings[0].provenance is EnumCompanionProvenance.UNKNOWN
        assert "search API 503" in findings[0].reason
        assert decide_contention(findings)[0] is True

    def test_files_failure_degrades_to_unknown(self) -> None:
        def _boom(_n: int) -> list[dict[str, object]]:
            raise OSError("connection reset")

        findings = find_open_companions(
            tickets=["OMN-15232"],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch="auto/mine-occ-autobind",
            search_issues=lambda _p: _search_payload(5115),
            get_pull=lambda _n: {"head": {"ref": "jonah/x"}, "labels": []},
            list_pr_files=_boom,
        )
        assert findings[0].provenance is EnumCompanionProvenance.UNKNOWN

    def test_candidate_count_is_capped(self) -> None:
        calls: list[int] = []

        def _pull(n: int) -> dict[str, object]:
            calls.append(n)
            return {"head": {"ref": f"jonah/{n}"}, "labels": []}

        find_open_companions(
            tickets=["OMN-15232"],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch="auto/mine-occ-autobind",
            search_issues=lambda _p: _search_payload(*range(1, 51)),
            get_pull=_pull,
            list_pr_files=lambda _n: [],
        )
        assert len(calls) == 10

    @pytest.mark.parametrize(
        ("ticket", "occ_pr", "head_ref"),
        [
            ("OMN-15229", 5089, "jonah/omn-15229-occ"),
            ("OMN-15218", 5107, "jonah/omn-15218-occ"),
            ("OMN-15232", 5115, "jonah/omn-15232-occ"),
        ],
    )
    def test_all_three_2026_07_27_occurrences_defer(
        self, ticket: str, occ_pr: int, head_ref: str
    ) -> None:
        """Replay of the three occurrences OMN-15247 records, as fixtures."""
        findings = find_open_companions(
            tickets=[ticket],
            occ_repo="OmniNode-ai/onex_change_control",
            own_branch="auto/omninode-ai-x-pr-1-occ-autobind",
            search_issues=lambda _p: _search_payload(occ_pr),
            get_pull=lambda _n: {"head": {"ref": head_ref}, "labels": []},
            list_pr_files=lambda _n: [{"filename": f"contracts/{ticket}.yaml"}],
        )
        assert decide_contention(findings)[0] is True


# ---------------------------------------------------------------------------
# resolve_red_ref — the merge base, not pr.base.sha
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveRedRef:
    def test_merged_pr_uses_the_squash_commit_first_parent(self) -> None:
        """The exact pair OMN-15247 re-verified for OMN-15232."""
        seen: list[str] = []

        def _commit(sha: str) -> dict[str, object]:
            seen.append(sha)
            return {"parents": [{"sha": _PARENT_SHA}, {"sha": "b" * 40}]}

        red = resolve_red_ref(
            pr_data={
                "merged": True,
                "merge_commit_sha": _MERGE_SHA,
                "head": {"sha": _HEAD_SHA},
                "base": {"sha": _BASE_TIP_SHA},
            },
            compare=lambda _b, _h: pytest.fail("compare must not be called for merged"),
            commit=_commit,
        )
        assert red == _PARENT_SHA
        assert seen == [_MERGE_SHA]

    def test_open_pr_uses_the_compare_merge_base_not_base_sha(self) -> None:
        red = resolve_red_ref(
            pr_data={
                "merged": False,
                "head": {"sha": _HEAD_SHA},
                "base": {"sha": _BASE_TIP_SHA},
            },
            compare=lambda b, h: (
                {"merge_base_commit": {"sha": _MERGE_BASE_SHA}}
                if (b, h) == (_BASE_TIP_SHA, _HEAD_SHA)
                else {}
            ),
            commit=lambda _s: pytest.fail("commit must not be called for an open PR"),
        )
        assert red == _MERGE_BASE_SHA
        assert red != _BASE_TIP_SHA

    @pytest.mark.parametrize(
        "pr_data",
        [
            {"merged": True, "merge_commit_sha": _MERGE_SHA},  # parents missing
            {"merged": False, "head": {}, "base": {"sha": _BASE_TIP_SHA}},
            {"merged": False, "head": {"sha": _HEAD_SHA}, "base": {}},
            {"merged": False, "head": {"sha": "not-a-sha"}, "base": {"sha": "x"}},
        ],
    )
    def test_unresolvable_returns_none(self, pr_data: dict[str, object]) -> None:
        assert (
            resolve_red_ref(
                pr_data=pr_data,
                compare=lambda _b, _h: {},
                commit=lambda _s: {},
            )
            is None
        )

    def test_open_pr_merge_base_absent_returns_none(self) -> None:
        assert (
            resolve_red_ref(
                pr_data={
                    "merged": False,
                    "head": {"sha": _HEAD_SHA},
                    "base": {"sha": _BASE_TIP_SHA},
                },
                compare=lambda _b, _h: {"status": "diverged"},
                commit=lambda _s: {},
            )
            is None
        )


# ---------------------------------------------------------------------------
# Shell/YAML safety guard (§B5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShellSafety:
    def test_the_canonical_generated_form_is_safe(self) -> None:
        check = build_content_read_check(
            repo="OmniNode-ai/omnimarket",
            path="src/omnimarket/handlers/handler_x.py",
            kind="class",
            symbol="HandlerX",
            head_sha=_MERGE_SHA,
        )
        assert is_shell_safe_check(check) is True

    @pytest.mark.parametrize(
        "check",
        [
            "",
            # No pinned ref at all.
            "gh api repos/o/r/contents/x.py --jq '.content' | base64 -d | grep -c 'class X'",
            # Two pinned refs — ambiguous for the corpus rewriter.
            f"gh api repos/o/r/contents/x.py?ref={_MERGE_SHA}&x=?ref={_PARENT_SHA} "
            "--jq '.content' | grep -c 'class X'",
            # Placeholder ref: the compliance runner has no ${SHA} token, so this
            # would run literally and could never be RED-derived.
            "gh api repos/o/r/contents/x.py?ref=${SHA} --jq '.content' | grep -c 'a'",
            # Metacharacter injected via the repo/path segment.
            f"gh api repos/o/r`whoami`/contents/x.py?ref={_MERGE_SHA} "
            "--jq '.content' | grep -c 'class X'",
        ],
    )
    def test_unsafe_forms_are_rejected(self, check: str) -> None:
        assert is_shell_safe_check(check) is False

    def test_over_length_check_is_rejected(self) -> None:
        long_path = "a/" * 300 + "x.py"
        check = build_content_read_check(
            repo="OmniNode-ai/omnimarket",
            path=long_path,
            kind="class",
            symbol="HandlerX",
            head_sha=_MERGE_SHA,
        )
        assert len(check) > MAX_CHECK_VALUE_LENGTH
        assert is_shell_safe_check(check) is False
