# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16536 AC#2: the git-automation demotion ratchet.

What this asserts
-----------------
No Linear ``GitAutomationState``, on any team, may resolve a ``completed``-type
ticket to a NON-``completed`` state.

Linear's ``GitAutomationState`` carries **no source-state predicate**. It names
one target state per git event and moves every linked issue there
unconditionally — regardless of the issue's current state, and regardless of
whether any closing keyword was used (a bare ``Refs:`` body mention, or a
``ready_for_review`` transition, is sufficient; both firing surfaces are
recorded on OMN-16536).

Because the transition is unconditional, "resolves a completed ticket to a
non-completed state" is a property of the *target* alone: any automation whose
target type is not ``completed`` will demote a verified-Done ticket the moment
it fires. So the ratchet is exactly:

    per team:  len(gitAutomationStates) == 0
               OR every automation targets a completed-type state

Anything else fails.

Relationship to the OMN-15373 guard (the mirror assertion)
----------------------------------------------------------
``git_automation_guard`` (OMN-15373) asserts the opposite polarity: no
automation may target a ``completed`` state, because that mints Done with no
evidence. The conjunction of the two guards is therefore
``len(gitAutomationStates) == 0`` on every team — which is precisely the state
OMN-16536 AC#1 established on 2026-08-27 by deleting all ten mappings across
teams OMN, CON and JON.

The two are deliberately kept as separate assertions rather than fused: they
are owned by different tickets, carry different exception registries, and a
future decision to accept one polarity must not silently relax the other.

Guarding the guard
------------------
A ratchet whose passing state is "we found nothing" is only trustworthy if it
can tell "nothing is configured" apart from "the probe did not work". Every
test below that expects a PASS on an empty automation set also proves the
positive control fired; every probe failure, enumeration failure, empty team
list, or truncated page FAILS.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_demotion_guard import (
    HandlerGitAutomationDemotionGuard,
    LinearPerTeamTransport,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_demotion_guard import (
    EnumDemotionVerdict,
    ModelDemotionAuditReport,
    ModelTeamAutomationProbe,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationState,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_demotion_guard import (
    audit_demotion_risk,
    evaluate_demotion_risk,
    render_demotion_report,
)

_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)

_TEAM_IDS = {
    "OMN": "9bdff6a3-f4ef-4ff7-b29a-6c4cf44371e6",
    "CON": "ef2198a9-350e-4a7a-adcf-fffe7ff6072a",
    "JON": "12081a78-cec9-40c4-9d4c-027186ac205f",
}


def _automation(
    *,
    team_key: str = "OMN",
    automation_id: str = "auto-1",
    event: str = "merge",
    state_name: str = "In Review",
    state_type: str = "started",
    readable: bool = True,
) -> ModelGitAutomationState:
    return ModelGitAutomationState(
        team_key=team_key,
        team_id=_TEAM_IDS.get(team_key, ""),
        automation_id=automation_id,
        event=event,
        state_id="state-1" if readable else "",
        state_name=state_name if readable else "",
        state_type=state_type if readable else "",
        state_readable=readable,
        target_branch=None,
    )


def _clean_team(team_key: str) -> ModelTeamAutomationProbe:
    """A team that was successfully probed and carries zero automations."""
    return ModelTeamAutomationProbe(
        team_key=team_key,
        team_id=_TEAM_IDS[team_key],
        probe_ok=True,
        automations=[],
        page_size=50,
    )


# ---------------------------------------------------------------------------
# Pure per-automation verdicts
# ---------------------------------------------------------------------------


class TestEvaluateDemotionRisk:
    """The unconditional-transition rule, one automation at a time."""

    @pytest.mark.unit
    def test_started_target_is_demotion_risk(self) -> None:
        """`merge -> In Review` demotes a verified-Done ticket. This is the
        exact mapping OMN-15373 installed as its own fix and OMN-16536 measured
        firing on five tickets at +2.3-3.2s."""
        finding = evaluate_demotion_risk(
            _automation(state_name="In Review", state_type="started")
        )
        assert finding.verdict is EnumDemotionVerdict.DEMOTION_RISK
        assert "completed" in finding.reason

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("state_name", "state_type"),
        [
            ("In Progress", "started"),
            ("Backlog", "backlog"),
            ("Todo", "unstarted"),
            ("Canceled", "canceled"),
            ("Triage", "triage"),
        ],
    )
    def test_every_non_completed_type_is_demotion_risk(
        self, state_name: str, state_type: str
    ) -> None:
        """No non-completed type is safe. Each one is a state a Done ticket can
        be knocked back into."""
        finding = evaluate_demotion_risk(
            _automation(state_name=state_name, state_type=state_type)
        )
        assert finding.verdict is EnumDemotionVerdict.DEMOTION_RISK

    @pytest.mark.unit
    def test_completed_target_is_clean_for_this_ratchet(self) -> None:
        """A completed-type target cannot demote a completed ticket, so it is
        CLEAN *for this ratchet*. It is simultaneously DRIFT for the OMN-15373
        guard — the two assertions are mirrors and both must be satisfied."""
        finding = evaluate_demotion_risk(
            _automation(state_name="Done", state_type="completed")
        )
        assert finding.verdict is EnumDemotionVerdict.CLEAN

    @pytest.mark.unit
    def test_unreadable_target_fails_closed(self) -> None:
        """An unreadable target is never CLEAN — an unreadable check is not a
        passing check."""
        finding = evaluate_demotion_risk(_automation(readable=False))
        assert finding.verdict is EnumDemotionVerdict.UNREADABLE

    @pytest.mark.unit
    def test_completed_type_match_is_case_and_whitespace_insensitive(self) -> None:
        """Linear returns lowercase types today; do not let a casing change flip
        the ratchet's polarity into silently passing everything."""
        finding = evaluate_demotion_risk(
            _automation(state_name="Done", state_type="  Completed  ")
        )
        assert finding.verdict is EnumDemotionVerdict.CLEAN


# ---------------------------------------------------------------------------
# The aggregate ratchet
# ---------------------------------------------------------------------------


class TestAuditDemotionRisk:
    """`len == 0 OR all completed`, with the positive control enforced."""

    @pytest.mark.unit
    def test_zero_automations_everywhere_passes_when_probe_is_proven(self) -> None:
        """The AC#1 post-state: all ten mappings deleted across OMN/CON/JON.
        This is the ratchet's steady green."""
        report = audit_demotion_risk(
            [_clean_team("OMN"), _clean_team("CON"), _clean_team("JON")],
            teams_enumerated=3,
            enumeration_ok=True,
            now=_NOW,
        )
        assert report.passed is True
        assert report.positive_control_ok is True
        assert report.demotion_risk_count == 0

    @pytest.mark.unit
    def test_single_demoting_automation_fails_the_whole_audit(self) -> None:
        """One re-added mapping through the settings UI is enough to fail."""
        omn = ModelTeamAutomationProbe(
            team_key="OMN",
            team_id=_TEAM_IDS["OMN"],
            probe_ok=True,
            automations=[_automation(event="merge", state_type="started")],
            page_size=50,
        )
        report = audit_demotion_risk(
            [omn, _clean_team("CON"), _clean_team("JON")],
            teams_enumerated=3,
            enumeration_ok=True,
            now=_NOW,
        )
        assert report.passed is False
        assert report.demotion_risk_count == 1

    @pytest.mark.unit
    def test_all_completed_targets_pass(self) -> None:
        """The second limb of the ratchet: a non-empty set is acceptable to
        THIS assertion when every target is completed-type."""
        omn = ModelTeamAutomationProbe(
            team_key="OMN",
            team_id=_TEAM_IDS["OMN"],
            probe_ok=True,
            automations=[
                _automation(automation_id="a", event="merge", state_type="completed"),
                _automation(automation_id="b", event="review", state_type="completed"),
            ],
            page_size=50,
        )
        report = audit_demotion_risk(
            [omn], teams_enumerated=1, enumeration_ok=True, now=_NOW
        )
        assert report.passed is True

    @pytest.mark.unit
    def test_the_ten_deleted_mappings_would_all_have_failed(self) -> None:
        """Regression against the AC#1 readback: every one of the ten mappings
        deleted on 2026-08-27 targeted a `started`-type state, so every one
        satisfies this ratchet's failure predicate. Deleting only the `merge`
        mapping would have left `draft`/`start`/`review` still demoting."""
        recorded = [
            ("OMN", "draft", "In Progress"),
            ("OMN", "start", "In Progress"),
            ("OMN", "review", "In Review"),
            ("OMN", "merge", "In Review"),
            ("CON", "start", "In Progress"),
            ("CON", "review", "In Review"),
            ("CON", "merge", "In Review"),
            ("JON", "start", "In Progress"),
            ("JON", "review", "In Review"),
            ("JON", "merge", "In Review"),
        ]
        probes = [
            ModelTeamAutomationProbe(
                team_key=key,
                team_id=_TEAM_IDS[key],
                probe_ok=True,
                automations=[
                    _automation(
                        team_key=key,
                        automation_id=f"{key}-{event}",
                        event=event,
                        state_name=name,
                        state_type="started",
                    )
                    for (k, event, name) in recorded
                    if k == key
                ],
                page_size=50,
            )
            for key in ("OMN", "CON", "JON")
        ]
        report = audit_demotion_risk(
            probes, teams_enumerated=3, enumeration_ok=True, now=_NOW
        )
        assert report.passed is False
        assert report.demotion_risk_count == 10

    @pytest.mark.unit
    def test_unreadable_automation_fails_the_audit(self) -> None:
        omn = ModelTeamAutomationProbe(
            team_key="OMN",
            team_id=_TEAM_IDS["OMN"],
            probe_ok=True,
            automations=[_automation(readable=False)],
            page_size=50,
        )
        report = audit_demotion_risk(
            [omn], teams_enumerated=1, enumeration_ok=True, now=_NOW
        )
        assert report.passed is False
        assert report.unreadable_count == 1


# ---------------------------------------------------------------------------
# Guarding the guard: the positive control
# ---------------------------------------------------------------------------


class TestPositiveControlFailsClosed:
    """An empty result may only read as PASS when the probe is proven live.

    This is the whole risk of a ratchet whose green state is "nothing found":
    a revoked token, a renamed field, or a silently-truncated page all produce
    the same empty set that a correct configuration does.
    """

    @pytest.mark.unit
    def test_team_enumeration_failure_fails(self) -> None:
        report = audit_demotion_risk(
            [],
            teams_enumerated=0,
            enumeration_ok=False,
            enumeration_error="401 Unauthorized",
            now=_NOW,
        )
        assert report.passed is False
        assert report.positive_control_ok is False
        assert "401" in report.failure_reason

    @pytest.mark.unit
    def test_zero_teams_enumerated_fails_even_though_nothing_is_drifted(self) -> None:
        """Zero teams is indistinguishable from a broken query or a token that
        cannot see the workspace. It must never read as 'no demotion found'."""
        report = audit_demotion_risk(
            [], teams_enumerated=0, enumeration_ok=True, now=_NOW
        )
        assert report.passed is False
        assert report.positive_control_ok is False

    @pytest.mark.unit
    def test_per_team_probe_error_fails_the_whole_audit(self) -> None:
        """One unreadable team is enough to fail: the other teams' cleanliness
        proves nothing about the one that could not be read."""
        broken = ModelTeamAutomationProbe(
            team_key="CON",
            team_id=_TEAM_IDS["CON"],
            probe_ok=False,
            probe_error="Query too complex",
            automations=[],
            page_size=50,
        )
        report = audit_demotion_risk(
            [_clean_team("OMN"), broken],
            teams_enumerated=2,
            enumeration_ok=True,
            now=_NOW,
        )
        assert report.passed is False
        assert "CON" in report.failure_reason

    @pytest.mark.unit
    def test_enumerated_team_missing_from_probes_fails(self) -> None:
        """Teams enumerated but not probed means the sweep silently skipped a
        team — the population shrank without anyone noticing."""
        report = audit_demotion_risk(
            [_clean_team("OMN")],
            teams_enumerated=3,
            enumeration_ok=True,
            now=_NOW,
        )
        assert report.passed is False
        assert report.positive_control_ok is False

    @pytest.mark.unit
    def test_full_page_of_automations_fails_as_possible_truncation(self) -> None:
        """A page returned exactly full cannot prove it was the last page. The
        nested org-wide query the OMN-15373 guard uses pages automations at 10
        with no pagination check — a team with 11 automations would hide one.
        Fail closed rather than audit a population we cannot prove is whole."""
        omn = ModelTeamAutomationProbe(
            team_key="OMN",
            team_id=_TEAM_IDS["OMN"],
            probe_ok=True,
            automations=[
                _automation(automation_id=f"a{i}", state_type="completed")
                for i in range(3)
            ],
            page_size=3,
        )
        report = audit_demotion_risk(
            [omn], teams_enumerated=1, enumeration_ok=True, now=_NOW
        )
        assert report.passed is False
        assert "truncat" in report.failure_reason.lower()


# ---------------------------------------------------------------------------
# The live handler, driven through a mock transport
# ---------------------------------------------------------------------------


class _MockTransport(LinearPerTeamTransport):
    """Records every GraphQL call so the per-team enumeration is provable."""

    def __init__(
        self,
        teams: list[dict[str, str]] | None = None,
        automations: dict[str, list[dict[str, object]]] | None = None,
        *,
        teams_error: Exception | None = None,
        team_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._teams = teams if teams is not None else []
        self._automations = automations or {}
        self._teams_error = teams_error
        self._team_errors = team_errors or {}
        self.team_calls: list[str] = []
        self.enumerate_calls = 0

    def enumerate_teams(self) -> list[dict[str, str]]:
        self.enumerate_calls += 1
        if self._teams_error is not None:
            raise self._teams_error
        return self._teams

    def fetch_team_automations(self, team_id: str) -> list[dict[str, object]]:
        self.team_calls.append(team_id)
        err = self._team_errors.get(team_id)
        if err is not None:
            raise err
        return self._automations.get(team_id, [])


def _live_shape(
    automation_id: str, event: str, name: str, type_: str
) -> dict[str, object]:
    """The verbatim Linear `GitAutomationState` node shape."""
    return {
        "id": automation_id,
        "event": event,
        "state": {"id": "s-" + automation_id, "name": name, "type": type_},
        "targetBranch": None,
    }


class TestHandlerPerTeamEnumeration:
    @pytest.mark.unit
    def test_enumerates_every_team_individually(self) -> None:
        """Per-team enumeration is a requirement, not an implementation detail:
        the nested `teams x gitAutomationStates` query's complexity is the
        PRODUCT of both page sizes and breaches Linear's 10000 cap at 50x50
        (measured 11565 on 2026-08-27). Splitting the read keeps each query's
        complexity constant as the workspace grows, and lets automations be
        paged at the full 50 instead of the 10 the nested form is forced to."""
        transport = _MockTransport(
            teams=[
                {"id": _TEAM_IDS["OMN"], "key": "OMN", "name": "Omninode"},
                {"id": _TEAM_IDS["CON"], "key": "CON", "name": "Contractors"},
                {"id": _TEAM_IDS["JON"], "key": "JON", "name": "JonahPrivate"},
            ],
        )
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)

        assert transport.enumerate_calls == 1
        assert transport.team_calls == [
            _TEAM_IDS["OMN"],
            _TEAM_IDS["CON"],
            _TEAM_IDS["JON"],
        ]
        assert report.passed is True

    @pytest.mark.unit
    def test_green_on_the_live_2026_08_27_shape(self) -> None:
        """Positive control recorded live on 2026-08-27: three teams, every
        `gitAutomationStates` empty — the AC#1 post-deletion state."""
        transport = _MockTransport(
            teams=[
                {"id": _TEAM_IDS[k], "key": k, "name": k} for k in ("OMN", "CON", "JON")
            ],
            automations={_TEAM_IDS[k]: [] for k in ("OMN", "CON", "JON")},
        )
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert report.passed is True
        assert report.positive_control_ok is True

    @pytest.mark.unit
    def test_red_when_a_demoting_mapping_is_re_added(self) -> None:
        """The scenario the ratchet exists for: someone re-adds `merge -> In
        Review` through the Linear settings UI."""
        transport = _MockTransport(
            teams=[{"id": _TEAM_IDS["OMN"], "key": "OMN", "name": "Omninode"}],
            automations={
                _TEAM_IDS["OMN"]: [
                    _live_shape("f39a0bc8", "merge", "In Review", "started")
                ]
            },
        )
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert report.passed is False
        assert report.demotion_risk_count == 1

    @pytest.mark.unit
    def test_api_error_on_enumeration_fails_closed(self) -> None:
        """An API error must FAIL the check, never pass silently."""
        transport = _MockTransport(teams_error=RuntimeError("Linear GraphQL error"))
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert report.passed is False
        assert report.positive_control_ok is False

    @pytest.mark.unit
    def test_api_error_on_one_team_fails_closed(self) -> None:
        transport = _MockTransport(
            teams=[
                {"id": _TEAM_IDS["OMN"], "key": "OMN", "name": "Omninode"},
                {"id": _TEAM_IDS["CON"], "key": "CON", "name": "Contractors"},
            ],
            team_errors={_TEAM_IDS["CON"]: RuntimeError("429 rate limited")},
        )
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert report.passed is False
        assert "CON" in report.failure_reason

    @pytest.mark.unit
    def test_handler_never_raises_on_probe_failure(self) -> None:
        """The scheduled runner needs a uniform exit path — a failing report,
        not an exception that reads as an infrastructure flake."""
        transport = _MockTransport(teams_error=OSError("connection reset"))
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert isinstance(report, ModelDemotionAuditReport)
        assert report.passed is False

    @pytest.mark.unit
    def test_malformed_state_block_is_unreadable_not_dropped(self) -> None:
        """Dropping an unparseable automation would silently shrink the audit
        population — the same failure as not checking at all."""
        transport = _MockTransport(
            teams=[{"id": _TEAM_IDS["OMN"], "key": "OMN", "name": "Omninode"}],
            automations={
                _TEAM_IDS["OMN"]: [
                    {
                        "id": "broken",
                        "event": "merge",
                        "state": None,
                        "targetBranch": None,
                    }
                ]
            },
        )
        report = HandlerGitAutomationDemotionGuard(transport=transport).handle(now=_NOW)
        assert report.passed is False
        assert report.unreadable_count == 1


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


class TestRenderDemotionReport:
    @pytest.mark.unit
    def test_render_states_the_result_and_the_control(self) -> None:
        report = audit_demotion_risk(
            [_clean_team("OMN")], teams_enumerated=1, enumeration_ok=True, now=_NOW
        )
        text = render_demotion_report(report)
        assert "PASS" in text
        assert "OMN-16536" in text
        # The control must be legible in the log, otherwise a green run is
        # indistinguishable from a run that checked nothing.
        assert "positive control" in text.lower()

    @pytest.mark.unit
    def test_render_names_each_demoting_mapping(self) -> None:
        omn = ModelTeamAutomationProbe(
            team_key="OMN",
            team_id=_TEAM_IDS["OMN"],
            probe_ok=True,
            automations=[_automation(event="merge", state_name="In Review")],
            page_size=50,
        )
        text = render_demotion_report(
            audit_demotion_risk(
                [omn], teams_enumerated=1, enumeration_ok=True, now=_NOW
            )
        )
        assert "FAIL" in text
        assert "merge" in text
        assert "In Review" in text
