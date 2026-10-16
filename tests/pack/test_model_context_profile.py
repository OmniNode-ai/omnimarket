"""ModelContextProfile validation: tail count, stable order, task classes (OMN-21106)."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import ValidationError

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.pack.model_context_profile import ModelContextProfile

F = EnumContextFactor


def _profile(**overrides: object) -> ModelContextProfile:
    values: dict[str, object] = {"model_id": "claude-code-session"}
    values.update(overrides)
    return ModelContextProfile.model_validate(values)


class TestConversationTailMessages:
    @pytest.mark.parametrize("field", ["required_factors", "optional_factors"])
    def test_selected_tail_without_count_fails(self, field: str) -> None:
        with pytest.raises(ValidationError, match="conversation_tail_messages"):
            _profile(**{field: (F.CONVERSATION_TAIL,)})

    @pytest.mark.parametrize("field", ["required_factors", "optional_factors"])
    def test_selected_tail_with_count_validates(self, field: str) -> None:
        profile = _profile(
            **{field: (F.CONVERSATION_TAIL,)}, conversation_tail_messages=5
        )
        assert profile.conversation_tail_messages == 5

    def test_unselected_tail_validates_with_field_absent(self) -> None:
        profile = _profile(required_factors=(F.OPEN_WORK,))
        assert profile.conversation_tail_messages is None

    def test_excluded_tail_is_not_selected(self) -> None:
        assert (
            _profile(excluded_factors=(F.CONVERSATION_TAIL,)).conversation_tail_messages
            is None
        )

    def test_zero_count_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _profile(
                optional_factors=(F.CONVERSATION_TAIL,), conversation_tail_messages=0
            )


class TestRoiRunnerProfileStillValidates:
    def test_model_id_and_full_enum_precedence(self) -> None:
        # handler_context_roi_runner builds exactly this profile.
        profile = ModelContextProfile(
            model_id="roi-runner", factor_precedence=tuple(EnumContextFactor)
        )
        assert profile.factor_precedence == tuple(EnumContextFactor)
        assert profile.stable_factors == ()
        assert profile.conversation_tail_messages is None


class TestSectionFourOneItemFiveFields:
    def test_profile_carries_every_declared_field(self) -> None:
        profile = _profile(
            injection_point="session.compact",
            binding_kind=EnumContextBindingKind.ADDITIVE,
            scopes=("process",),
            adr_topics=("architecture",),
            stable_factors=(F.LANE_RULES,),
            selection_task_class="context_selection",
            extraction_task_class="extraction_proposer",
            summary_task_class="tail_summary",
            latency_budget_ms=800,
        )
        assert profile.injection_point == "session.compact"
        assert profile.binding_kind is EnumContextBindingKind.ADDITIVE
        assert profile.scopes == ("process",)
        assert profile.adr_topics == ("architecture",)
        assert profile.stable_factors == (F.LANE_RULES,)
        assert profile.selection_task_class == "context_selection"
        assert profile.extraction_task_class == "extraction_proposer"
        assert profile.summary_task_class == "tail_summary"
        assert profile.latency_budget_ms == 800
        assert profile.task_kind_rules == ()

    def test_binding_kind_values(self) -> None:
        assert {k.value for k in EnumContextBindingKind} == {
            "replace",
            "additive",
            "inline",
        }


class TestFactorPrecedenceConstraints:
    def test_non_stable_before_stable_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="stable"):
            _profile(
                stable_factors=(F.LANE_RULES,),
                factor_precedence=(F.OPEN_WORK, F.LANE_RULES),
            )

    def test_stable_first_validates(self) -> None:
        profile = _profile(
            stable_factors=(F.LANE_RULES, F.INVARIANTS),
            factor_precedence=(F.LANE_RULES, F.INVARIANTS, F.OPEN_WORK),
        )
        assert profile.factor_precedence[0] is F.LANE_RULES

    def test_stable_factor_must_be_a_stable_tier_factor(self) -> None:
        with pytest.raises(ValidationError, match="stable"):
            _profile(stable_factors=(F.OPEN_WORK,))

    def test_model_written_factor_not_last_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="model-written"):
            _profile(factor_precedence=(F.TAIL_SUMMARY, F.OPEN_WORK))

    def test_model_written_factor_last_validates(self) -> None:
        profile = _profile(factor_precedence=(F.OPEN_WORK, F.TAIL_SUMMARY))
        assert profile.factor_precedence[-1] is F.TAIL_SUMMARY

    def test_model_written_factor_alone_validates(self) -> None:
        assert _profile(factor_precedence=(F.TAIL_SUMMARY,)).factor_precedence == (
            F.TAIL_SUMMARY,
        )


class TestTaskClassFieldsNeverNameAModel:
    @pytest.mark.parametrize(
        "field", ["selection_task_class", "extraction_task_class", "summary_task_class"]
    )
    @pytest.mark.parametrize(
        "value", ["acme-model-7b", "vendor/model", "model_2", "Context_Summary", "v1.5"]
    )
    def test_model_id_shaped_value_is_refused(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError, match=field):
            _profile(**{field: value})

    @pytest.mark.parametrize(
        "field", ["selection_task_class", "extraction_task_class", "summary_task_class"]
    )
    def test_task_class_name_validates(self, field: str) -> None:
        assert (
            getattr(_profile(**{field: "context_compaction"}), field)
            == "context_compaction"
        )

    def test_profile_model_id_label_is_refused_as_task_class(self) -> None:
        with pytest.raises(ValidationError, match="summary_task_class"):
            _profile(model_id="local", summary_task_class="local")


class TestLatencyBudget:
    def test_additive_profile_may_declare_it(self) -> None:
        profile = _profile(
            binding_kind=EnumContextBindingKind.ADDITIVE, latency_budget_ms=500
        )
        assert profile.latency_budget_ms == 500

    @pytest.mark.parametrize(
        "binding_kind",
        [EnumContextBindingKind.REPLACE, EnumContextBindingKind.INLINE, None],
    )
    def test_non_additive_profile_declaring_it_is_refused(
        self, binding_kind: EnumContextBindingKind | None
    ) -> None:
        with pytest.raises(ValidationError, match="latency_budget_ms"):
            _profile(binding_kind=binding_kind, latency_budget_ms=500)

    def test_non_additive_profile_without_it_validates(self) -> None:
        assert (
            _profile(binding_kind=EnumContextBindingKind.REPLACE).latency_budget_ms
            is None
        )


class TestContentHash:
    def test_equal_profiles_have_equal_hash(self) -> None:
        assert (
            _profile(token_budget=100).content_hash()
            == _profile(token_budget=100).content_hash()
        )

    def test_any_field_change_changes_hash(self) -> None:
        assert (
            _profile(token_budget=100).content_hash()
            != _profile(token_budget=101).content_hash()
        )
