"""Profile resolver by name: shipped YAML data, typed failures, equal hash (OMN-21106)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
    ModelContextProfileResolveResult,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

F = EnumContextFactor
_PROFILES_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "pack" / "profiles"
)


@pytest.fixture
def promoted_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile-data tests run with explicitly promoted fixture task classes."""
    path = tmp_path / "routing.yaml"
    entry = {
        "gateway_exposure": "internal",
        "selection": {"priority": 0, "phrases": []},
    }
    path.write_text(
        yaml.safe_dump(
            {
                "task_classes": {
                    "extraction_proposer": entry,
                    "context_compaction": entry,
                    "context_selection": entry,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(path))


def _resolve(
    name: str, profiles_dir: Path | None = None
) -> ModelContextProfileResolveResult:
    handler = (
        HandlerContextProfileResolver(profiles_dir=profiles_dir)
        if profiles_dir
        else HandlerContextProfileResolver()
    )
    return handler.handle(ModelContextProfileResolveRequest(profile_name=name))


def test_unknown_name_is_profile_not_found() -> None:
    result = _resolve("no_such_profile")
    assert result.status is EnumContextProfileResolveStatus.FAILED
    assert result.profile_name == "no_such_profile"
    assert result.failure_class is EnumContextPackFailure.PROFILE_NOT_FOUND
    assert result.profile is None
    assert result.profile_content_hash is None


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "", "Upper", "x.yaml"])
def test_name_that_is_not_a_bare_profile_name_is_not_found(name: str) -> None:
    # A name never reaches the filesystem as a path.
    try:
        request = ModelContextProfileResolveRequest(profile_name=name)
    except ValueError:
        return
    result = HandlerContextProfileResolver().handle(request)
    assert result.failure_class is EnumContextPackFailure.PROFILE_NOT_FOUND


def test_same_name_resolves_equal_object_and_equal_hash(
    promoted_contract: None,
) -> None:
    first = _resolve("compaction_orchestrator")
    second = _resolve("compaction_orchestrator")
    assert first.status is EnumContextProfileResolveStatus.OK
    assert first.profile == second.profile
    assert first.profile_content_hash == second.profile_content_hash
    assert first.profile is not None
    assert first.profile_content_hash == first.profile.content_hash()
    assert len(first.profile_content_hash or "") == 64


def test_compaction_orchestrator_profile_data(promoted_contract: None) -> None:
    result = _resolve("compaction_orchestrator")
    profile = result.profile
    assert isinstance(profile, ModelContextProfile)
    assert profile.required_factors == (
        F.OPERATOR_RULINGS,
        F.OPERATOR_WORDS_UNRECORDED,
        F.ORCHESTRATION_STATE,
        F.OPEN_WORK,
    )
    assert set(profile.optional_factors) == {
        F.REPO_STATE,
        F.CONVERSATION_TAIL,
        F.TRANSCRIPT_EXTRACTS,
        F.TAIL_SUMMARY,
    }
    assert profile.binding_kind is EnumContextBindingKind.REPLACE
    assert profile.injection_point == "session.compact"
    assert profile.token_budget == 16000
    assert profile.conversation_tail_messages is not None
    assert profile.factor_precedence[-1] is F.TAIL_SUMMARY
    assert profile.latency_budget_ms is None


def test_every_shipped_profile_file_resolves_by_its_stem(
    promoted_contract: None,
) -> None:
    files = sorted(_PROFILES_DIR.glob("*.yaml"))
    assert files, "no shipped profile data"
    for path in files:
        result = _resolve(path.stem)
        assert result.status is EnumContextProfileResolveStatus.OK, (
            path.name,
            result.errors,
        )


def test_invalid_profile_file_is_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        yaml.safe_dump({"model_id": "m", "optional_factors": ["conversation_tail"]}),
        encoding="utf-8",
    )
    result = _resolve("bad", tmp_path)
    assert result.failure_class is EnumContextPackFailure.INVALID_PROFILE
    assert any("conversation_tail_messages" in e for e in result.errors)


def test_unparseable_profile_file_is_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("model_id: [unclosed", encoding="utf-8")
    assert (
        _resolve("broken", tmp_path).failure_class
        is EnumContextPackFailure.INVALID_PROFILE
    )


def test_unknown_key_in_profile_file_is_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text(
        yaml.safe_dump({"model_id": "m", "surprise": 1}), encoding="utf-8"
    )
    assert (
        _resolve("extra", tmp_path).failure_class
        is EnumContextPackFailure.INVALID_PROFILE
    )


def test_profile_file_is_resolved_from_the_directory_given(tmp_path: Path) -> None:
    (tmp_path / "mine.yaml").write_text(
        yaml.safe_dump({"model_id": "m"}), encoding="utf-8"
    )
    result = _resolve("mine", tmp_path)
    assert result.status is EnumContextProfileResolveStatus.OK
    assert result.profile is not None
    assert result.profile.model_id == "m"
    assert _resolve("compaction_orchestrator", tmp_path).failure_class is (
        EnumContextPackFailure.PROFILE_NOT_FOUND
    )


def test_contract_declares_one_terminal_topic_and_the_resolve_outputs() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_context_profile_resolver_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    terminal = "onex.evt.omnimarket.context-profile-resolve-completed.v1"
    assert contract["terminal_event"] == terminal
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert set(contract["outputs"]) == {
        "status",
        "profile_name",
        "profile",
        "profile_content_hash",
        "failure_class",
        "errors",
    }


def test_session_resume_profile_data(promoted_contract: None) -> None:
    # Plan section 4.4 row session_resume (OMN-21119).
    result = _resolve("session_resume")
    profile = result.profile
    assert isinstance(profile, ModelContextProfile)
    assert set(profile.required_factors) == {
        F.OPEN_WORK,
        F.OPERATOR_RULINGS,
        F.ORCHESTRATION_STATE,
    }
    assert set(profile.optional_factors) == {
        F.TRANSCRIPT_EXTRACTS,
        F.TAIL_SUMMARY,
        F.PLAN_EXCERPTS,
    }
    assert profile.binding_kind is EnumContextBindingKind.INLINE
    assert profile.factor_precedence[0] is F.OPERATOR_RULINGS
    assert profile.factor_precedence[-1] is F.TAIL_SUMMARY
    assert profile.extraction_task_class is not None
    assert profile.summary_task_class is not None
    assert profile.conversation_tail_messages is None
    assert profile.latency_budget_ms is None


def test_delegation_payload_local_profile_data() -> None:
    # Plan section 4.4 row delegation_payload_local (OMN-21119): nothing is
    # required, so a source outage never blocks a delegation.
    result = _resolve("delegation_payload_local")
    profile = result.profile
    assert isinstance(profile, ModelContextProfile)
    assert profile.required_factors == ()
    assert profile.optional_factors == (
        F.INVARIANTS,
        F.CANONICAL_SHAPES,
        F.EXEMPLAR,
        F.ARCHITECTURE_PATTERNS,
    )
    assert set(profile.stable_factors) == set(profile.optional_factors)
    assert profile.scopes == ("delegation",)
    assert profile.binding_kind is EnumContextBindingKind.INLINE
    assert profile.token_budget == 8000
    # No model role in v5.
    assert profile.selection_task_class is None
    assert profile.extraction_task_class is None
    assert profile.summary_task_class is None
