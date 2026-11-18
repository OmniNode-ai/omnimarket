# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-brief context profile and the pack it builds (seven tiers, a hash, recorded rungs)."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_lane_brief_tier import (
    EnumLaneBriefRung,
    EnumLaneBriefTier,
    EnumLaneBriefTierMode,
)
from omnimarket.models.context_pack_orchestrator.model_lane_brief_context import (
    ModelLaneBriefContextPack,
    ModelLaneBriefContextRequest,
    ModelLaneBriefExemplarCandidate,
    ModelLaneBriefExemplarChoice,
    ModelLaneBriefReviewComment,
    ModelLaneBriefSummary,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_lane_brief_context_pack import (
    HandlerLaneBriefContextPack,
    LaneBriefContextRefusedError,
    LaneBriefRungError,
    load_lane_brief_risks,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_source_ledger_effect import (
    HandlerContextSourceLedger,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
PROFILE_PATH = SRC / "pack" / "profiles" / "lane_brief_context.yaml"
ALL_TIERS = tuple(EnumLaneBriefTier)
REVIEW_TEXT = (
    "Finding 1: the handler reads the ledger twice.\n"
    "Finding 2: the refusal names no field.\n"
    + "A long, unsummarised review paragraph. "
    * 40
)


class _Rung:
    def __init__(
        self,
        name: EnumLaneBriefRung,
        *,
        fail: str | None = None,
        path: str = "src/a.py",
        excerpt: str = "def shape():",
    ) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0
        self._path = path
        self._excerpt = excerpt

    def select(
        self,
        request: ModelLaneBriefContextRequest,
        candidates: Sequence[ModelLaneBriefExemplarCandidate],
    ) -> ModelLaneBriefExemplarChoice:
        self.calls += 1
        if self.fail is not None:
            raise LaneBriefRungError(self.fail)
        return ModelLaneBriefExemplarChoice(
            path=self._path,
            excerpt=self._excerpt,
            model="test-model",
            run_id="fixture-exemplar",
        )

    def summarize(
        self, request: ModelLaneBriefContextRequest, facts: str
    ) -> ModelLaneBriefSummary:
        return ModelLaneBriefSummary(
            text="Earlier lanes worked on the same change.",
            model="test-model",
            run_id="fixture-summary",
        )


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def read(self, request: ModelLaneBriefContextRequest) -> str:
        self.calls += 1
        return self.text


def _rungs(**overrides: _Rung) -> list[_Rung]:
    return [
        overrides.get(name.value, _Rung(name))
        for name in (
            EnumLaneBriefRung.LOCAL,
            EnumLaneBriefRung.GLM,
        )
    ]


def _request(**changes: object) -> ModelLaneBriefContextRequest:
    base: dict[str, object] = {
        "ticket": "OMN-1",
        "lane": "new-lane",
        "task": "add a check",
        "files_to_touch": ("src/a.py", "tests/test_a.py"),
        "review_comments": (
            ModelLaneBriefReviewComment(comment_id="c1", text=REVIEW_TEXT),
        ),
        "exemplar_candidates": (
            ModelLaneBriefExemplarCandidate(
                path="src/a.py", text="x = 1\ndef shape():\n    return x\n"
            ),
            ModelLaneBriefExemplarCandidate(path="src/b.py", text="y = 2\n"),
        ),
        "ledger_rows": (
            "2026-11-16T01:00:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | first",
            "2026-11-16T01:05:00Z | CLAIM | lane=other | ticket=OMN-2 | unrelated",
            "2026-11-16T01:06:00Z | STATUS | lane=other | touched src/a.py | by file",
        ),
    }
    base.update(changes)
    return ModelLaneBriefContextRequest.model_validate(base)


def _handler(
    rungs: Sequence[_Rung] | None = None,
    *,
    graph: _Reader | None = None,
    tests: _Reader | None = None,
    profiles_dir: Path | None = None,
    ledger: HandlerContextSourceLedger | None = None,
) -> HandlerLaneBriefContextPack:
    return HandlerLaneBriefContextPack(
        rungs=_rungs() if rungs is None else rungs,
        readers={
            EnumLaneBriefTier.GRAPH_ANSWERS: graph or _Reader("importers: b.py"),
            EnumLaneBriefTier.TESTS_AND_CI: tests or _Reader("ci: validate-plugin"),
        },
        profiles_dir=profiles_dir,
        ledger=ledger,
    )


def _profile_data() -> dict[str, object]:
    loaded = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_lane_brief_context_tiers_profile_declares_seven_budgeted_tiers() -> None:
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name="lane_brief_context")
    )
    assert result.status is EnumContextProfileResolveStatus.OK, result.errors
    profile = result.profile
    assert profile is not None
    assert [spec.tier for spec in profile.tier_specs] == list(ALL_TIERS)
    assert all(spec.token_budget > 0 for spec in profile.tier_specs)
    assert sum(s.token_budget for s in profile.tier_specs) <= profile.token_budget
    modes = {spec.tier: spec.mode for spec in profile.tier_specs}
    assert modes[EnumLaneBriefTier.EXEMPLAR] is EnumLaneBriefTierMode.MODEL_SELECTED
    assert [
        t for t, m in modes.items() if m is EnumLaneBriefTierMode.MODEL_SELECTED
    ] == [EnumLaneBriefTier.EXEMPLAR, EnumLaneBriefTier.LANE_HISTORY]


def test_lane_brief_context_tiers_other_profiles_keep_no_tier_specs() -> None:
    for name in ("lane_brief", "lane_brief_section"):
        result = HandlerContextProfileResolver().handle(
            ModelContextProfileResolveRequest(profile_name=name)
        )
        assert result.profile is not None
        assert result.profile.tier_specs == ()


@pytest.mark.parametrize("tier", ALL_TIERS, ids=lambda t: t.value)
def test_lane_brief_context_tiers_profile_missing_a_tier_is_refused(
    tier: EnumLaneBriefTier,
) -> None:
    data = _profile_data()
    specs = data["tier_specs"]
    assert isinstance(specs, list)
    data["tier_specs"] = [s for s in specs if s["tier"] != tier.value]
    assert len(data["tier_specs"]) == 6
    with pytest.raises(ValidationError, match=tier.value):
        ModelContextProfile.model_validate(data)


def test_lane_brief_context_tiers_profile_budgets_stay_inside_the_profile_budget() -> (
    None
):
    data = _profile_data()
    data["token_budget"] = 100
    data["context_window_tokens"] = 200000
    with pytest.raises(ValidationError, match="token_budget"):
        ModelContextProfile.model_validate(data)


def test_lane_brief_context_tiers_resolver_refuses_a_file_missing_a_tier(
    tmp_path: Path,
) -> None:
    data = _profile_data()
    specs = data["tier_specs"]
    assert isinstance(specs, list)
    data["tier_specs"] = specs[:-1]
    (tmp_path / "lane_brief_context.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(LaneBriefContextRefusedError, match="risks"):
        _handler(profiles_dir=tmp_path).handle(_request())


def test_lane_brief_context_tiers_pack_with_no_final_pack_hash_is_refused() -> None:
    built = _handler().handle(_request())
    data = built.model_dump(mode="json")
    del data["final_pack_hash"]
    with pytest.raises(ValidationError, match="final_pack_hash"):
        ModelLaneBriefContextPack.model_validate(data)
    data["final_pack_hash"] = ""
    with pytest.raises(ValidationError, match="final_pack_hash"):
        ModelLaneBriefContextPack.model_validate(data)
    data["final_pack_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="final_pack_hash"):
        ModelLaneBriefContextPack.model_validate(data)


def test_lane_brief_context_tiers_pack_carries_every_tier_and_its_hash() -> None:
    built = _handler().handle(_request())
    assert built.final_pack_hash == hashlib.sha256(built.body.encode()).hexdigest()
    assert [t.tier for t in built.tiers] == list(ALL_TIERS)
    for tier in ALL_TIERS:
        assert f"## {tier.value}" in built.body
    assert "src/a.py" in built.body
    assert "def shape():" in built.body


def test_lane_brief_context_tiers_review_findings_are_whole_never_summarised() -> None:
    built = _handler().handle(_request())
    assert REVIEW_TEXT in built.body


def test_lane_brief_context_tiers_review_findings_over_budget_are_refused_not_cut() -> (
    None
):
    huge = ModelLaneBriefReviewComment(comment_id="c9", text="word " * 200_000)
    with pytest.raises(LaneBriefContextRefusedError, match="review_findings"):
        _handler().handle(_request(review_comments=(huge,)))


def test_lane_brief_context_tiers_lane_history_is_the_ticket_and_file_rows_only() -> (
    None
):
    built = _handler().handle(_request())
    assert "| first" in built.body
    assert "| by file" in built.body
    assert "| unrelated" not in built.body


def test_lane_brief_context_tiers_risks_name_the_four_classes_each_with_a_source() -> (
    None
):
    risks = load_lane_brief_risks()
    assert {r.risk_class for r in risks} == {
        "frozen-repo-growth",
        "branch-name-refusal",
        "install-refusal",
        "suite-timeout",
    }
    assert all(r.source.strip() for r in risks)
    built = _handler().handle(_request())
    for risk in risks:
        assert risk.source in built.body


def test_lane_brief_context_tiers_risk_with_no_source_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "risks.yaml"
    path.write_text(
        yaml.safe_dump(
            {"risks": [{"risk_class": "suite-timeout", "rule": "x", "source": " "}]}
        )
    )
    with pytest.raises(LaneBriefContextRefusedError, match="source"):
        load_lane_brief_risks(path)


def test_lane_brief_context_fallback_deterministic_tiers_record_no_model_call() -> None:
    rungs = _rungs()
    graph, tests = _Reader("g"), _Reader("t")
    built = _handler(rungs, graph=graph, tests=tests).handle(_request())
    for record in built.tiers:
        if record.tier in {EnumLaneBriefTier.EXEMPLAR, EnumLaneBriefTier.LANE_HISTORY}:
            continue
        assert record.mode is EnumLaneBriefTierMode.DETERMINISTIC
        assert record.model_called is False
        assert record.rungs == ()
    assert graph.calls == 1
    assert tests.calls == 1
    assert [r.calls for r in rungs] == [1, 0]


def test_lane_brief_context_fallback_first_rung_failure_is_recorded_with_why() -> None:
    rungs = _rungs(local=_Rung(EnumLaneBriefRung.LOCAL, fail="lab host timed out"))
    built = _handler(rungs).handle(_request())
    exemplar = next(t for t in built.tiers if t.tier is EnumLaneBriefTier.EXEMPLAR)
    assert exemplar.model_called is True
    assert [(a.rung, a.outcome) for a in exemplar.rungs] == [
        (EnumLaneBriefRung.LOCAL, "fell_back"),
        (EnumLaneBriefRung.GLM, "selected"),
    ]
    assert "lab host timed out" in (exemplar.rungs[0].reason or "")
    assert [r.calls for r in rungs] == [1, 1]


def test_lane_brief_context_fallback_walks_local_then_jev_then_glm() -> None:
    rungs = [
        _Rung(EnumLaneBriefRung.LOCAL, fail="a"),
        _Rung(EnumLaneBriefRung.JEV, fail="b"),
        _Rung(EnumLaneBriefRung.GLM),
    ]
    built = _handler(rungs).handle(_request())
    exemplar = built.tiers[0]
    assert [attempt.rung for attempt in exemplar.rungs] == [
        EnumLaneBriefRung.LOCAL,
        EnumLaneBriefRung.JEV,
        EnumLaneBriefRung.GLM,
    ]
    assert [attempt.outcome for attempt in exemplar.rungs] == [
        "fell_back",
        "fell_back",
        "selected",
    ]


def test_lane_brief_context_fallback_an_invented_choice_falls_back() -> None:
    rungs = _rungs(
        local=_Rung(EnumLaneBriefRung.LOCAL, path="src/not_a_candidate.py"),
        glm=_Rung(EnumLaneBriefRung.GLM, excerpt="text the file never held"),
    )
    built = _handler(rungs).handle(_request())
    exemplar = next(t for t in built.tiers if t.tier is EnumLaneBriefTier.EXEMPLAR)
    reasons = [a.reason for a in exemplar.rungs if a.outcome == "fell_back"]
    assert len(reasons) == 2
    assert "not a candidate" in (reasons[0] or "")
    assert "verbatim" in (reasons[1] or "")


def test_lane_brief_context_fallback_every_rung_failing_marks_exemplar_unassembled() -> (
    None
):
    rungs = [_Rung(r.name, fail="down") for r in _rungs()]
    built = _handler(rungs).handle(_request())
    assert built.tiers[0].status == "unassembled"
    assert "every delegation rung fell back" in (built.tiers[0].reason or "")


def test_lane_brief_context_fallback_rungs_out_of_order_are_refused() -> None:
    with pytest.raises(LaneBriefContextRefusedError, match="local, jev, glm"):
        _handler(list(reversed(_rungs()))).handle(_request())


# --- OMN-24732: review findings on the first head --------------------------------------------------

_CONTRACT_PATH = SRC / "nodes" / "node_context_pack_orchestrator" / "contract.yaml"
_LANE_BRIEF_CAPABILITY = "context.pack.lane_brief"


def _lane_brief_route() -> tuple[dict[str, Any], dict[str, Any]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    routes = [
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry["event_model"]["name"] == ModelLaneBriefContextRequest.__name__
    ]
    assert len(routes) == 1, "the lane-brief request has no handler_routing entry"
    return contract, routes[0]


def _resolve(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def test_lane_brief_context_routing_contract_routes_the_capability_to_the_handler() -> (
    None
):
    contract, route = _lane_brief_route()
    assert _LANE_BRIEF_CAPABILITY in [c["name"] for c in contract["capabilities"]]
    assert route["handler"]["name"] == HandlerLaneBriefContextPack.__name__
    assert route["handler"]["module"] == HandlerLaneBriefContextPack.__module__
    assert route["event_model"]["module"] == ModelLaneBriefContextRequest.__module__
    # no topic of its own (a new topic needs a broker grant): the node's one command topic carries it,
    # and the runtime tells the two commands apart by payload type
    assert "topic" not in route
    assert contract["handler_routing"]["routing_strategy"] == "payload_type_match"
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert route["message_category"] == "command"


def test_lane_brief_context_routing_command_path_builds_the_pack() -> None:
    """The way the runtime does it: read the route, build the handler, validate the payload, handle it."""
    _, route = _lane_brief_route()
    event_model = _resolve(route["event_model"]["module"], route["event_model"]["name"])
    handler_cls = _resolve(route["handler"]["module"], route["handler"]["name"])
    payload = _request().model_dump(mode="json")
    command = event_model.model_validate(payload)
    bound = handler_cls(
        rungs=_rungs(),
        readers={
            EnumLaneBriefTier.GRAPH_ANSWERS: _Reader("importers: b.py"),
            EnumLaneBriefTier.TESTS_AND_CI: _Reader("ci: validate-plugin"),
        },
    )
    built = bound.handle(command)
    assert built.final_pack_hash == hashlib.sha256(built.body.encode()).hexdigest()


def _history_of(**changes: object) -> str:
    built = _handler().handle(_request(**changes))
    body = built.body
    return body.split("## lane_history\n", 1)[1].split("\n\n## risks", 1)[0]


def test_lane_history_boundary_ticket_id_is_not_a_prefix_of_another_ticket() -> None:
    history = _history_of(
        ticket="OMN-1",
        files_to_touch=("src/a.py",),
        ledger_rows=(
            "2026-11-16T01:00:00Z | CLAIM | lane=a | ticket=OMN-1 | wanted",
            "2026-11-16T01:01:00Z | CLAIM | lane=b | ticket=OMN-12 | not wanted",
            "2026-11-16T01:02:00Z | CLAIM | lane=c | parent=OMN-1, ticket=OMN-9 | wanted too",
        ),
    )
    assert "| wanted" in history
    assert "wanted too" in history
    assert "not wanted" not in history


def test_lane_history_boundary_path_is_not_the_tail_of_a_longer_path() -> None:
    history = _history_of(
        ticket="OMN-1",
        files_to_touch=("src/a.py",),
        ledger_rows=(
            "2026-11-16T01:00:00Z | STATUS | lane=a | touched src/a.py | wanted",
            "2026-11-16T01:01:00Z | STATUS | lane=b | touched tests/src/a.pyi | not wanted",
            "2026-11-16T01:02:00Z | STATUS | lane=c | touched src/a.py.bak | not wanted either",
            "2026-11-16T01:03:00Z | STATUS | lane=d | touched (src/a.py), done | wanted too",
        ),
    )
    assert "| wanted" in history
    assert "wanted too" in history
    assert "not wanted" not in history


@pytest.mark.parametrize("empty", ["", "   "])
def test_lane_history_boundary_an_empty_path_in_files_to_touch_is_refused(
    empty: str,
) -> None:
    with pytest.raises(ValidationError, match="files_to_touch"):
        _request(files_to_touch=("src/a.py", empty))


def _over_budget_excerpt() -> ModelLaneBriefExemplarCandidate:
    return ModelLaneBriefExemplarCandidate(
        path="src/big.py", text="def shape():\n" + "    pass\n" * 6000
    )


def test_exemplar_budget_over_budget_excerpt_makes_the_rung_fall_back_with_a_reason() -> (
    None
):
    big = _over_budget_excerpt()
    rungs = _rungs(
        local=_Rung(EnumLaneBriefRung.LOCAL, path=big.path, excerpt=big.text)
    )
    built = _handler(rungs).handle(
        _request(
            exemplar_candidates=(
                big,
                ModelLaneBriefExemplarCandidate(
                    path="src/a.py", text="x = 1\ndef shape():\n    return x\n"
                ),
            )
        )
    )
    exemplar = next(t for t in built.tiers if t.tier is EnumLaneBriefTier.EXEMPLAR)
    assert [(a.rung, a.outcome) for a in exemplar.rungs] == [
        (EnumLaneBriefRung.LOCAL, "fell_back"),
        (EnumLaneBriefRung.GLM, "selected"),
    ]
    assert "budget" in (exemplar.rungs[0].reason or "")
    assert exemplar.token_estimate <= exemplar.token_budget
    assert [r.calls for r in rungs] == [1, 1]


def test_exemplar_budget_every_rung_over_budget_marks_exemplar_unassembled() -> None:
    big = _over_budget_excerpt()
    rungs = [_Rung(r.name, path=big.path, excerpt=big.text) for r in _rungs()]
    built = _handler(rungs).handle(_request(exemplar_candidates=(big,)))
    assert built.tiers[0].status == "unassembled"
    assert all("budget" in (a.reason or "") for a in built.tiers[0].rungs)
    assert [r.calls for r in rungs] == [1, 1]


def test_exemplar_budget_empty_candidate_list_marks_exemplar_unassembled_without_a_rung_call() -> (
    None
):
    rungs = _rungs()
    built = _handler(rungs).handle(_request(exemplar_candidates=()))
    assert built.tiers[0].status == "unassembled"
    assert [r.calls for r in rungs] == [0, 0]


def _reversed_profile_data() -> dict[str, object]:
    data = _profile_data()
    specs = data["tier_specs"]
    assert isinstance(specs, list)
    data["tier_specs"] = list(reversed(specs))
    return data


def test_tier_specs_order_reversed_profile_is_refused_by_validation() -> None:
    with pytest.raises(ValidationError, match="order"):
        ModelContextProfile.model_validate(_reversed_profile_data())


def test_tier_specs_order_reversed_profile_is_refused_typed_with_no_rung_call(
    tmp_path: Path,
) -> None:
    (tmp_path / "lane_brief_context.yaml").write_text(
        yaml.safe_dump(_reversed_profile_data())
    )
    rungs = _rungs()
    result = HandlerContextProfileResolver(tmp_path).handle(
        ModelContextProfileResolveRequest(profile_name="lane_brief_context")
    )
    assert result.status is not EnumContextProfileResolveStatus.OK
    with pytest.raises(LaneBriefContextRefusedError, match="order"):
        _handler(rungs, profiles_dir=tmp_path).handle(_request())
    assert [r.calls for r in rungs] == [0, 0]


def test_lane_brief_context_tiers_an_empty_tier_is_an_explicit_empty_line() -> None:
    built = _handler().handle(_request(review_comments=(), files_to_touch=()))
    for tier in ("review_findings", "files_to_touch"):
        section = built.body.split(f"## {tier}\n", 1)[1].split("\n\n## ", 1)[0]
        assert section.strip(), f"{tier} is blank"
    assert _handler().handle(_request()).final_pack_hash != built.final_pack_hash


def test_lane_brief_context_tiers_an_unsupported_token_estimation_method_is_refused(
    tmp_path: Path,
) -> None:
    data = _profile_data()
    data["token_estimation_method"] = "tiktoken_exact"
    (tmp_path / "lane_brief_context.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(LaneBriefContextRefusedError, match="token_estimation_method"):
        _handler(profiles_dir=tmp_path).handle(_request())


def test_lane_brief_context_tiers_required_risk_classes_are_checked_for_a_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "risks.yaml"
    path.write_text(
        yaml.safe_dump(
            {"risks": [{"risk_class": "suite-timeout", "rule": "x", "source": "s"}]}
        )
    )
    with pytest.raises(LaneBriefContextRefusedError, match="lacks classes"):
        load_lane_brief_risks(path)


# --- OMN-24768: review findings on the second head ---------------------------------------------------


class _RaisingRung(_Rung):
    """A rung whose transport raises (not a ``LaneBriefRungError``) instead of choosing."""

    def __init__(self, name: EnumLaneBriefRung, error: Exception) -> None:
        super().__init__(name)
        self._error = error

    def select(
        self,
        request: ModelLaneBriefContextRequest,
        candidates: Sequence[ModelLaneBriefExemplarCandidate],
    ) -> ModelLaneBriefExemplarChoice:
        self.calls += 1
        raise self._error


@pytest.mark.parametrize(
    "error", [TimeoutError("lab rung timed out"), ConnectionError("reset")]
)
def test_rung_timeout_falls_back_with_a_recorded_reason(error: Exception) -> None:
    rungs = _rungs(local=_RaisingRung(EnumLaneBriefRung.LOCAL, error))
    built = _handler(rungs).handle(_request())
    exemplar = next(t for t in built.tiers if t.tier is EnumLaneBriefTier.EXEMPLAR)
    assert [(a.rung, a.outcome) for a in exemplar.rungs] == [
        (EnumLaneBriefRung.LOCAL, "fell_back"),
        (EnumLaneBriefRung.GLM, "selected"),
    ]
    assert type(error).__name__ in (exemplar.rungs[0].reason or "")
    assert rungs[1].calls == 1


def test_rung_timeout_on_every_rung_records_fallback_reasons() -> None:
    rungs = [_RaisingRung(r.name, TimeoutError()) for r in _rungs()]
    built = _handler(rungs).handle(_request())
    assert built.tiers[0].status == "unassembled"
    assert all("TimeoutError" in (a.reason or "") for a in built.tiers[0].rungs)
    assert [r.calls for r in rungs] == [1, 1]


def test_duplicate_candidate_path_is_refused_by_the_request_model() -> None:
    twice = (
        ModelLaneBriefExemplarCandidate(path="src/a.py", text="x = 1\n"),
        ModelLaneBriefExemplarCandidate(path="src/a.py", text="y = 2\n"),
    )
    with pytest.raises(ValidationError, match=r"exemplar_candidates.*src/a\.py"):
        _request(exemplar_candidates=twice)


@pytest.mark.parametrize("blank", [" ", "\n\t "])
def test_blank_excerpt_is_refused_by_the_choice_model(blank: str) -> None:
    with pytest.raises(ValidationError, match="excerpt"):
        ModelLaneBriefExemplarChoice(path="src/a.py", excerpt=blank)


def test_lane_history_boundary_repository_prefixed_and_dot_slash_paths_count() -> None:
    history = _history_of(
        ticket="OMN-9",
        files_to_touch=("src/a.py",),
        ledger_rows=(
            "2026-11-16T01:00:00Z | STATUS | lane=a | touched omnimarket/src/a.py | wanted prefixed",
            "2026-11-16T01:01:00Z | STATUS | lane=b | touched ./src/a.py | wanted dot-slash",
            "2026-11-16T01:02:00Z | STATUS | lane=c | touched other/src/a.py | not wanted",
            "2026-11-16T01:03:00Z | STATUS | lane=d | touched tests/src/a.py | not wanted either",
        ),
    )
    assert "wanted prefixed" in history
    assert "wanted dot-slash" in history
    assert "not wanted" not in history


def test_lane_history_boundary_a_prefixed_needle_matches_the_bare_path_row() -> None:
    history = _history_of(
        ticket="OMN-9",
        files_to_touch=("./src/a.py",),
        ledger_rows=(
            "2026-11-16T01:00:00Z | STATUS | lane=a | touched src/a.py | hit",
        ),
    )
    assert "| hit" in history


def test_lane_history_boundary_ticket_named_by_its_contract_file_counts() -> None:
    history = _history_of(
        ticket="OMN-1",
        files_to_touch=("src/a.py",),
        ledger_rows=(
            "2026-11-16T01:00:00Z | STATUS | lane=a | wrote contracts/OMN-1.yaml | wanted",
            "2026-11-16T01:01:00Z | STATUS | lane=b | wrote contracts/OMN-12.yaml | not wanted",
        ),
    )
    assert "| wanted" in history
    assert "not wanted" not in history


def _routes_accepting(payload: dict[str, Any]) -> list[str]:
    contract, _ = _lane_brief_route()
    accepting: list[str] = []
    for entry in contract["handler_routing"]["handlers"]:
        model = _resolve(entry["event_model"]["module"], entry["event_model"]["name"])
        try:
            model.model_validate(payload)
        except ValidationError:
            continue
        accepting.append(entry["event_model"]["name"])
    return accepting


def test_route_exclusion_each_payload_validates_against_exactly_one_route_event_model() -> (
    None
):
    lane_brief = _request().model_dump(mode="json")
    generic = {
        "request": {"profile_name": "compaction_orchestrator", "session_id": "s-1"},
        "access": {"tenant_id": "tenant-a"},
    }
    assert _routes_accepting(lane_brief) == [ModelLaneBriefContextRequest.__name__]
    assert _routes_accepting(generic) == ["ModelContextPackCommand"]


def test_routing_reply_goes_to_the_nodes_declared_terminal_topics() -> None:
    contract, _ = _lane_brief_route()
    terminal = contract["runtime_dispatch"]["terminal_events"]
    assert terminal["success"] in contract["externally_consumed_topics"]
    assert terminal["failure"] in contract["externally_consumed_topics"]
    assert contract["runtime_dispatch"]["command_topic"] not in terminal.values()


def test_lane_brief_context_tiers_runtime_binds_the_seven_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CLAUDE_PLUGIN_ROOT",
        "OMNI_PLUGIN_ROOT",
        "ONEX_LANE_BRIEF_DELEGATION_STEP",
    ):
        monkeypatch.delenv(name, raising=False)
    built = HandlerLaneBriefContextPack().handle(
        ModelLaneBriefContextRequest(
            lane="new-lane", ticket="OMN-12", task="Wire the source"
        )
    )
    assert [record.tier for record in built.tiers] == list(EnumLaneBriefTier)
    assert built.final_pack_hash == hashlib.sha256(built.body.encode()).hexdigest()
    assert "unassembled (" in built.body


def test_lane_brief_rung_binding_records_each_judgement() -> None:
    local = _Rung(EnumLaneBriefRung.LOCAL)
    built = _handler([local]).handle(_request())
    exemplar, history = built.tiers[0], built.tiers[5]
    assert exemplar.rungs[-1].rung is EnumLaneBriefRung.LOCAL
    assert exemplar.rungs[-1].run_id == "fixture-exemplar"
    assert history.rungs[-1].rung is EnumLaneBriefRung.LOCAL
    assert history.rungs[-1].run_id == "fixture-summary"
    assert "assembler: rung=local" in built.body


def test_lane_brief_rung_binding_refuses_claude_before_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_lane_brief_context_pack as handler_module

    def unexpected_hash(_: bytes) -> str:
        pytest.fail("a Claude judgement reached the pack hash")

    monkeypatch.setattr(handler_module, "_hash_body", unexpected_hash, raising=False)
    claude = _Rung(EnumLaneBriefRung.SONNET)
    with pytest.raises(LaneBriefContextRefusedError, match="Claude") as refusal:
        _handler([claude]).handle(_request())
    assert refusal.value.kind.value == "claude_assembler"
    assert claude.calls == 0


def test_lane_brief_ledger_tier_tags_earlier_terminals(tmp_path: Path) -> None:
    path = tmp_path / "ledger.md"
    path.write_text(
        "\n".join(
            (
                "2026-11-16T01:00:00Z | CLAIM | lane=prior-a | ticket=OMN-2 | pr=omnimarket#12 | worked",
                "2026-11-16T01:01:00Z | TERMINAL | lane=prior-a | outcome=blocked | blocker_class=install | first end",
                "2026-11-16T01:02:00Z | TERMINAL | lane=prior-b | ticket=OMN-2 | pr=omnimarket#12 | outcome=partial | second end",
            )
        )
    )
    source = HandlerContextSourceLedger.from_markdown(path, tenant_ids=("test",))
    built = _handler(ledger=source).handle(
        _request(ledger_rows=None, pull_request="omnimarket#12"),
        tenant_id="test",
    )
    for lane, outcome, blocker, time in (
        ("prior-a", "blocked", "install", "01:01"),
        ("prior-b", "partial", "none", "01:02"),
    ):
        assert any(
            all(
                tag in line
                for tag in (
                    f"lane={lane}",
                    "ticket=OMN-2",
                    "pr=omnimarket#12",
                    "kind=TERMINAL",
                    f"outcome={outcome}",
                    f"time=2026-11-16T{time}:00Z",
                    f"blocker_class={blocker}",
                )
            )
            for line in built.body.splitlines()
        )


@pytest.mark.parametrize(
    ("changes", "row"),
    [
        ({"ticket": "OMN-12"}, "ticket=OMN-1"),
        ({"ticket": "OMN-12"}, "ticket=XYZ-OMN-12"),
        ({"files_to_touch": ()}, "arbitrary history"),
        ({"files_to_touch": ("src/a.py",)}, "src/a.py/child"),
    ],
)
def test_lane_brief_ledger_selection_rejects_nonmatching_tokens(
    changes: dict[str, object], row: str
) -> None:
    built = _handler().handle(
        _request(
            ledger_rows=(
                f"2026-11-16T01:00:00Z | STATUS | lane=prior | {row} | UNRELATED",
            ),
            **changes,
        )
    )
    assert "UNRELATED" not in built.body


def test_lane_brief_ledger_selection_matches_pr_at_token_boundaries() -> None:
    built = _handler().handle(
        _request(
            pull_request="omnimarket#12",
            ledger_rows=(
                "2026-11-16T01:00:00Z | TERMINAL | lane=prior-a | pr=omnimarket#12 | outcome=partial | EXACT",
                "2026-11-16T01:01:00Z | TERMINAL | lane=prior-b | pr=omnimarket#123 | outcome=partial | PREFIX",
            ),
        )
    )
    assert "EXACT" in built.body
    assert "PREFIX" not in built.body


def test_lane_brief_ledger_unassembled_distinguishes_missing_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.md"
    path.write_text(
        "2026-11-16T01:06:00Z | STATUS | lane=other | touched src/a.py | by file\n"
    )
    source = HandlerContextSourceLedger.from_markdown(path, tenant_ids=("test",))
    handler = _handler(ledger=source)
    request = _request(ledger_rows=None)
    up = handler.handle(request, tenant_id="test")
    assert "by file" in up.body
    path.unlink()
    down = handler.handle(request, tenant_id="test")
    assert "## lane_history\nunassembled (" in down.body
    assert down.tiers[5].status == "unassembled"
    assert "ledger read failed" in (down.tiers[5].reason or "")


def test_lane_brief_rung_binding_dispatch_emits_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.models.context_pack_orchestrator import ModelContextPackCommand
    from omnimarket.nodes.node_context_pack_orchestrator.handlers import (
        handler_lane_brief_context_pack as assembly,
    )
    from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator_runtime import (
        HandlerContextPackOrchestratorRuntime,
    )

    def unexpected_hash(_: bytes) -> str:
        pytest.fail("the refusal reached the pack hash")

    class ClaudeAnswer(_Rung):
        def select(
            self,
            request: ModelLaneBriefContextRequest,
            candidates: Sequence[ModelLaneBriefExemplarCandidate],
        ) -> ModelLaneBriefExemplarChoice:
            result = super().select(request, candidates)
            return result.model_copy(update={"model": "claude-sonnet-4"})

    monkeypatch.setattr(assembly, "_hash_body", unexpected_hash)
    runtime = HandlerContextPackOrchestratorRuntime(
        sources={}, lane_brief=_handler([ClaudeAnswer(EnumLaneBriefRung.LOCAL)])
    )
    result = runtime.handle(
        ModelContextPackCommand.model_validate(
            {
                "request": {
                    "profile_name": "lane_brief_context",
                    "lane_brief": _request(ledger_rows=None).model_dump(mode="json"),
                },
                "access": {"tenant_id": "test"},
            }
        )
    )
    assert result.status.value == "failed"
    assert result.lane_brief_refusal is not None
    assert result.lane_brief_refusal.value == "claude_assembler"
    assert result.final_pack_hash is None
