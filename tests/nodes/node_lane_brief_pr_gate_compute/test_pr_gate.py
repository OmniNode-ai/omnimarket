# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief gate's PR-opening check: the ``Opens PR in`` line, the shared WIP refusal, the mode.

Every repository name and id below is an invented fixture.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.wip_admission_fill_gate import fill_wip_gate
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_mode import (
    EnumLaneBriefPrGateMode,
)
from omnimarket.models.lane_brief_pr_gate.enum_lane_brief_pr_gate_verdict import (
    EnumLaneBriefPrGateVerdict as Verdict,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_config import (
    ModelLaneBriefPrGateConfig,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_request import (
    ModelLaneBriefPrGateRequest,
)
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_result import (
    ModelLaneBriefPrGateResult,
)
from omnimarket.models.wip_gate import EnumWipEvidenceWriter, EnumWipExemptionKind
from omnimarket.nodes.node_lane_brief_pr_gate_compute.handlers.handler_lane_brief_pr_gate import (
    HandlerLaneBriefPrGate,
)
from omnimarket.nodes.node_lane_brief_pr_gate_compute.overlay import (
    OVERLAY_ID,
    load_lane_brief_pr_gate_config,
)
from omnimarket.nodes.node_wip_gate_reducer.overlay import load_wip_policy
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    overlay_doc as admission_overlay_doc,
)
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    pr as admission_pr,
)
from tests.nodes.node_wip_admission_compute.test_wip_admission import (
    watcher as admission_watcher,
)
from tests.nodes.node_wip_gate_reducer.test_wip_gate import (
    DISPATCH,
    NOW,
    REPO_A,
    REPO_B,
    SWARM_LANE,
    _evidence,
    _exemption,
    _overlay_doc,
    _policy,
    _state,
)

pytestmark = pytest.mark.unit

OBSERVE = ModelLaneBriefPrGateConfig(mode=EnumLaneBriefPrGateMode.OBSERVE)
ENFORCE = ModelLaneBriefPrGateConfig(
    mode=EnumLaneBriefPrGateMode.ENFORCE, citation="gate run over the live briefs"
)
FULL = _state()  # nine open in REPO_A, limit five
ROOM = _state(open_n=2, drafts=0)


def brief(opens: str | None = REPO_A) -> str:
    line = f"Opens PR in: {opens}\n" if opens is not None else ""
    return f"# Lane demo: Wire it\nRequired engine: codex\n{line}Ticket: OMN-21440\n"


def check(
    text: str,
    *,
    config: ModelLaneBriefPrGateConfig | None = OBSERVE,
    state: dict[str, Any] | None = None,
    enforce_wip: bool = True,
    **extra: Any,
) -> ModelLaneBriefPrGateResult:
    handler = HandlerLaneBriefPrGate(
        config=config, policy=_policy(enforce="fixture ruling" if enforce_wip else "")
    )
    return handler.handle(
        ModelLaneBriefPrGateRequest(
            brief=text,
            dispatch_id=DISPATCH,
            now=NOW,
            state=ROOM if state is None else state,
            **extra,
        )
    )


# AC2, AC8: the line


def test_ac2_a_pr_brief_with_no_opens_line_is_reported_under_observe() -> None:
    result = check(brief(None), config=OBSERVE)
    assert result.verdict is Verdict.REPORTED
    assert [issue.line for issue in result.issues] == ["Opens PR in"]


def test_ac2_a_pr_brief_with_no_opens_line_is_refused_under_enforce() -> None:
    result = check(brief(None), config=ENFORCE)
    assert result.verdict is Verdict.REFUSED
    assert result.issues[0].detail == "no such line"


def test_a_brief_that_states_the_line_twice_or_empty_is_not_admitted() -> None:
    twice = brief() + f"Opens PR in: {REPO_B}\n"
    assert check(twice, config=ENFORCE).verdict is Verdict.REFUSED
    assert check(brief(""), config=ENFORCE).verdict is Verdict.REFUSED


def test_a_line_inside_a_code_fence_is_not_metadata() -> None:
    fenced = f"# Lane x: y\n```\nOpens PR in: {REPO_A}\n```\n"
    assert check(fenced, config=ENFORCE).verdict is Verdict.REFUSED


def test_a_brief_that_opens_no_pr_is_admitted_without_the_wip_rule() -> None:
    result = check(brief("none"), config=ENFORCE, state=FULL)
    assert result.verdict is Verdict.ADMITTED
    assert result.wip is None


def test_a_brief_under_the_limit_is_admitted() -> None:
    result = check(brief(), config=ENFORCE)
    assert result.verdict is Verdict.ADMITTED
    assert result.wip is not None
    assert result.wip.open == 2


# AC3: the mode is not the request's


def test_ac3_a_request_that_carries_a_mode_field_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="mode"):
        ModelLaneBriefPrGateRequest.model_validate(
            {
                "brief": brief(),
                "dispatch_id": DISPATCH,
                "now": NOW,
                "mode": "observe",
            }
        )


def test_ac3_a_request_cannot_downgrade_an_injected_enforce_configuration() -> None:
    text = brief(None)
    handler = HandlerLaneBriefPrGate(config=ENFORCE, policy=_policy())
    request = ModelLaneBriefPrGateRequest(
        brief=text, dispatch_id=DISPATCH, now=NOW, config=OBSERVE, policy=_policy("")
    )
    assert handler.handle(request).verdict is Verdict.REFUSED


def test_ac3_a_request_may_make_an_observing_deployment_stricter() -> None:
    handler = HandlerLaneBriefPrGate(config=OBSERVE, policy=_policy())
    request = ModelLaneBriefPrGateRequest(
        brief=brief(None), dispatch_id=DISPATCH, now=NOW, config=ENFORCE
    )
    assert handler.handle(request).verdict is Verdict.REFUSED


def test_a_gate_with_no_configuration_refuses_naming_the_key() -> None:
    result = HandlerLaneBriefPrGate(policy=_policy()).handle(
        ModelLaneBriefPrGateRequest(brief=brief(), dispatch_id=DISPATCH, now=NOW)
    )
    assert result.verdict is Verdict.REFUSED
    assert result.issues[0].line == "lane_brief_pr_gate.mode"


def test_a_gate_with_no_wip_policy_refuses_a_pr_brief() -> None:
    result = HandlerLaneBriefPrGate(config=ENFORCE).handle(
        ModelLaneBriefPrGateRequest(brief=brief(), dispatch_id=DISPATCH, now=NOW)
    )
    assert result.verdict is Verdict.REFUSED
    assert result.issues[0].line == "wip_limits"


# AC4: the WIP rule


def test_ac4_a_brief_for_a_repository_at_its_limit_is_refused_when_wip_enforces() -> (
    None
):
    result = check(brief(), config=OBSERVE, state=FULL)
    assert result.verdict is Verdict.REFUSED
    assert result.wip is not None
    assert result.wip.reason.value == "at_limit"


def test_ac4_a_brief_at_the_limit_is_reported_when_wip_does_not_enforce() -> None:
    result = check(brief(), state=FULL, enforce_wip=False)
    assert result.verdict is Verdict.REPORTED


def test_ac4_an_exemption_the_brief_claims_alone_is_refused() -> None:
    claimed = brief() + "Exempt: swarm_lease\nWIP exempt: true\n"
    assert check(claimed, state=FULL).verdict is Verdict.REFUSED


def test_ac4_an_exemption_on_an_unrelated_pr_is_refused() -> None:
    unrelated = _exemption(kind=EnumWipExemptionKind.OPEN_PR, pr_number=4242)
    result = check(brief(), state=FULL, exemption=unrelated)
    assert result.verdict is Verdict.REFUSED


def test_ac4_an_expired_lease_is_refused() -> None:
    expired = _exemption(expires_at=NOW - timedelta(minutes=1))
    result = check(
        brief(), state=FULL, exemption=expired, authorized_swarm_lanes=(SWARM_LANE,)
    )
    assert result.verdict is Verdict.REFUSED


def test_a_valid_lease_is_admitted() -> None:
    result = check(
        brief(),
        state=FULL,
        exemption=_exemption(),
        authorized_swarm_lanes=(SWARM_LANE,),
    )
    assert result.verdict is Verdict.ADMITTED
    assert result.wip is not None
    assert result.wip.reason.value == "exempt"


# AC6: override evidence


def test_ac6_valid_override_evidence_passes_as_admitted_override() -> None:
    result = check(brief(), state=FULL, override=_evidence())
    assert result.verdict is Verdict.ADMITTED_OVERRIDE


def test_ac6_forged_override_evidence_is_refused() -> None:
    forged = _evidence(written_by=EnumWipEvidenceWriter.DISPATCHER)
    assert check(brief(), state=FULL, override=forged).verdict is Verdict.REFUSED
    wrong_key = _evidence(evidence_key="override:slot-9:4")
    assert check(brief(), state=FULL, override=wrong_key).verdict is Verdict.REFUSED


# AC7, AC8: the overlay


def write_overlay(root: Path, overlay_id: str, doc: dict[str, Any]) -> dict[str, str]:
    folder = root / overlay_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_ac7_enforce_without_a_citation_is_refused() -> None:
    with pytest.raises(ValidationError, match="citation"):
        ModelLaneBriefPrGateConfig(mode=EnumLaneBriefPrGateMode.ENFORCE)
    with pytest.raises(ValidationError, match="citation"):
        ModelLaneBriefPrGateConfig(mode=EnumLaneBriefPrGateMode.ENFORCE, citation=" ")


def test_ac8_the_overlay_sets_the_mode_and_the_citation(tmp_path: Path) -> None:
    env = write_overlay(tmp_path, OVERLAY_ID, {"mode": "observe"})
    config = load_lane_brief_pr_gate_config(env)
    assert config.mode is EnumLaneBriefPrGateMode.OBSERVE
    assert check(brief(None), config=config).verdict is Verdict.REPORTED
    env = write_overlay(
        tmp_path, OVERLAY_ID, {"mode": "enforce", "citation": "run 7 over 9 briefs"}
    )
    config = load_lane_brief_pr_gate_config(env)
    assert config.citation == "run 7 over 9 briefs"
    assert check(brief(None), config=config).verdict is Verdict.REFUSED


def test_ac8_enforce_with_no_citation_in_the_overlay_is_refused(
    tmp_path: Path,
) -> None:
    env = write_overlay(tmp_path, OVERLAY_ID, {"mode": "enforce"})
    with pytest.raises(NodeOverlayError, match="citation"):
        load_lane_brief_pr_gate_config(env)


def test_ac8_a_missing_overlay_refuses_naming_the_id(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match=OVERLAY_ID):
        load_lane_brief_pr_gate_config({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


# AC5: the fill and this gate change together


def test_ac5_the_fill_and_the_gate_enforce_together_when_enforce_ruling_is_set(
    tmp_path: Path,
) -> None:
    def verdicts(ruling: str) -> tuple[bool, bool]:
        root = tmp_path / ("on" if ruling else "off")
        admission = {**admission_overlay_doc(), "enforce_ruling": ruling}
        limits = {**_overlay_doc(), "enforce_ruling": ruling}
        write_overlay(root, "wip_admission", admission)
        env = write_overlay(root, "wip_limits", limits)
        watcher = admission_watcher(*(admission_pr(REPO_A, i) for i in range(1, 12)))
        refusal, _ = fill_wip_gate(
            ledger_tickets=None, watcher_state=watcher, now=NOW, environ=env
        )
        fill_refused = refusal({"kind": "ticket", "repo": REPO_A, "key": "k"})
        gate = HandlerLaneBriefPrGate(
            config=OBSERVE, policy=load_wip_policy(env)
        ).handle(
            ModelLaneBriefPrGateRequest(
                brief=brief(), dispatch_id=DISPATCH, now=NOW, state=FULL
            )
        )
        return (
            bool(fill_refused and fill_refused.startswith("wip-limit: REFUSE")),
            gate.verdict is Verdict.REFUSED,
        )

    assert verdicts("fixture ruling stamp") == (True, True)
    assert verdicts("") == (False, False)
