# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S13 — the multi-event publish seam flag, defined twice and imported never.

``ONEX_MULTI_EVENT_PUBLISH_SEAM`` gates whether the completion leg (S2) may
publish multiple correlated events for one command. Both runtimes read it:
``omnibase_core.runtime.runtime_local_adapter`` on the ``RuntimeLocal`` path
and ``omnibase_infra.runtime.auto_wiring.handler_wiring`` on the auto-wiring
path. Each defines its own module-level string constant; neither imports the
other's. ``handler_wiring``'s own comment states the intent — "the two runtimes
agree" — but nothing mechanically holds them to it.

That is exactly the class of seam a per-side unit test cannot see: rename the
literal on one side and both suites stay green while the flag silently splits
in two, one runtime fanning out and the other warn-dropping. The goldens below
cross-import both real constants and both real truthy-parsers and prove they
agree on the literal AND on every parse decision — including the fail-safe that
an unset flag means OFF on both sides.

Environment mutation is scoped through ``monkeypatch``, so the parsers are
driven for real (they read ``os.environ`` directly) without leaking state.
"""

from __future__ import annotations

from typing import Final

import pytest
from omnibase_core.runtime.runtime_local_adapter import (
    ENV_MULTI_EVENT_PUBLISH_SEAM as CORE_FLAG_NAME,
)
from omnibase_core.runtime.runtime_local_adapter import (
    multi_event_publish_seam_enabled as core_seam_enabled,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ENV_MULTI_EVENT_PUBLISH_SEAM as INFRA_FLAG_NAME,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    multi_event_seam_enabled as infra_seam_enabled,
)

from omnimarket.seams.models.model_seam_projection import ModelSeamProjection
from tests.seam_goldens.harness import (
    UNVERSIONED_MODEL,
    EnumSeamProjectionRole,
    assert_regenerable,
    assert_registry_classification,
    consumer_projection,
    model_identity,
    observed_projection_from_mapping,
    producer_projection,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

# The DECLARED literal — pinned here, deliberately not imported. The whole seam
# is "two runtimes independently define this exact string", so the registry's
# claim about it has to be written down somewhere that neither runtime can move.
# The OBSERVED side is CORE_FLAG_NAME / INFRA_FLAG_NAME, imported for real: a
# rename on either side alone turns that side's observed key field into
# ABSENT_FROM_WIRE and fails its leg.
_DECLARED_FLAG_LITERAL: Final[str] = "ONEX_MULTI_EVENT_PUBLISH_SEAM"

# An env var crosses as a plain string; there is no envelope model here. Both
# sides record the builtin type identity, which each side's observation derives
# from its own real constant rather than from this declaration.
_DECLARED_FLAG_CARRIER: Final[str] = "builtins.str"

# Every string the goldens drive through both parsers. Covers the documented
# truthy set, obvious falsy values, and the casing/whitespace variants the
# ``.strip().lower()`` normalisation is supposed to absorb identically on both
# sides — a divergence in normalisation is as breaking as a renamed literal.
_PARSE_CASES: tuple[str, ...] = (
    "1",
    "true",
    "yes",
    "on",
    "TRUE",
    "  on  ",
    "0",
    "false",
    "no",
    "off",
    "",
    "maybe",
    "2",
)


class TestTheLiteralIsByteIdentical:
    def test_slice_row_is_a_local_runtime_edge(self) -> None:
        edge = slice_edge("S13")
        assert edge.traversed
        assert edge.registry_classification == "MATCHED_UNTESTED"

    def test_both_runtimes_name_the_same_env_var(self) -> None:
        assert CORE_FLAG_NAME == INFRA_FLAG_NAME

    def test_the_literal_is_the_expected_canonical_name(self) -> None:
        """Pin the value itself so a coordinated rename is still a decision."""

        assert CORE_FLAG_NAME == "ONEX_MULTI_EVENT_PUBLISH_SEAM"

    def test_the_constants_are_defined_independently_not_re_exported(self) -> None:
        """The seam only exists because neither side imports the other.

        If a future refactor makes one module re-export the other's constant,
        the duplication seam is genuinely closed — and this test failing is
        the signal to re-derive the registry rather than a regression.
        """

        core_module = core_seam_enabled.__module__
        infra_module = infra_seam_enabled.__module__
        assert core_module != infra_module
        assert core_module.startswith("omnibase_core.")
        assert infra_module.startswith("omnibase_infra.")


class TestBothParsersAgreeOnEveryValue:
    """Identical literals are not enough — the decisions must match too."""

    @pytest.mark.parametrize("value", _PARSE_CASES)
    def test_parsers_agree(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CORE_FLAG_NAME, value)

        assert core_seam_enabled() == infra_seam_enabled(), (
            f"runtimes disagree on ONEX_MULTI_EVENT_PUBLISH_SEAM={value!r}: "
            f"core={core_seam_enabled()} infra={infra_seam_enabled()}"
        )

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  on  "])
    def test_documented_truthy_values_enable_on_both_sides(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CORE_FLAG_NAME, value)

        assert core_seam_enabled() is True
        assert infra_seam_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe", "2"])
    def test_everything_else_stays_off_on_both_sides(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CORE_FLAG_NAME, value)

        assert core_seam_enabled() is False
        assert infra_seam_enabled() is False

    def test_unset_flag_defaults_off_on_both_sides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default-OFF is the documented fail-safe for this seam.

        A default that diverged would be the worst version of this defect: one
        runtime fanning out correlated events in production while the other
        warn-drops them, with no operator having set anything at all.
        """

        monkeypatch.delenv(CORE_FLAG_NAME, raising=False)

        assert core_seam_enabled() is False
        assert infra_seam_enabled() is False


def _declared() -> tuple[ModelSeamProjection, ModelSeamProjection]:
    """The registry's claim about this seam, written from the pinned literal."""

    declared_producer = producer_projection(
        edge_id="S13",
        topic=_DECLARED_FLAG_LITERAL,
        envelope_model=_DECLARED_FLAG_CARRIER,
        envelope_version=UNVERSIONED_MODEL,
        key_fields=((_DECLARED_FLAG_LITERAL, "str"),),
    )
    declared_consumer = consumer_projection(
        edge_id="S13",
        topic=_DECLARED_FLAG_LITERAL,
        envelope_model=_DECLARED_FLAG_CARRIER,
        envelope_version=UNVERSIONED_MODEL,
        key_fields=((_DECLARED_FLAG_LITERAL, "str"),),
    )
    return declared_producer, declared_consumer


class TestS13RegistryMatch:
    def test_registry_match_is_regenerable_for_the_driven_flag(self) -> None:
        """Declared = the pinned literal. Observed = the two real constants.

        Neither observed projection is derived from the declaration: each is
        built from the constant its own runtime actually exports, keyed by the
        declared name. Rename ``ENV_MULTI_EVENT_PUBLISH_SEAM`` in core and the
        producer leg goes red; rename it in infra and the consumer leg does.
        That asymmetry is impossible to express when both sides are the same
        object, which is what this call site used to do.
        """

        declared_producer, declared_consumer = _declared()

        verdict = run_registry_match(
            edge_id="S13",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_mapping(
                edge_id="S13",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=CORE_FLAG_NAME,
                mapping={CORE_FLAG_NAME: CORE_FLAG_NAME},
                field_names=(_DECLARED_FLAG_LITERAL,),
                envelope_model=model_identity(type(CORE_FLAG_NAME)),
            ),
            observed_consumer=observed_projection_from_mapping(
                edge_id="S13",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=INFRA_FLAG_NAME,
                mapping={INFRA_FLAG_NAME: INFRA_FLAG_NAME},
                field_names=(_DECLARED_FLAG_LITERAL,),
                envelope_model=model_identity(type(INFRA_FLAG_NAME)),
            ),
        )

        assert_registry_classification("S13", verdict)
        assert_regenerable("S13", verdict)

    def test_a_renamed_core_constant_would_fail_the_producer_leg(self) -> None:
        """Negative control on the OBSERVED leg, not just on leg 1.

        Simulates core renaming its constant while infra keeps the old name.
        The observed producer no longer carries the declared key, so leg 2 goes
        red while leg 1 and leg 3 stay green — the precise signature of a
        one-sided rename, and a result the old ``x == x`` call site could never
        produce.
        """

        declared_producer, declared_consumer = _declared()
        renamed = f"{_DECLARED_FLAG_LITERAL}_V2"

        verdict = run_registry_match(
            edge_id="S13",
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_projection_from_mapping(
                edge_id="S13",
                role=EnumSeamProjectionRole.PRODUCER,
                topic=renamed,
                mapping={renamed: renamed},
                field_names=(_DECLARED_FLAG_LITERAL,),
                envelope_model=_DECLARED_FLAG_CARRIER,
            ),
            observed_consumer=observed_projection_from_mapping(
                edge_id="S13",
                role=EnumSeamProjectionRole.CONSUMER,
                topic=INFRA_FLAG_NAME,
                mapping={INFRA_FLAG_NAME: INFRA_FLAG_NAME},
                field_names=(_DECLARED_FLAG_LITERAL,),
                envelope_model=model_identity(type(INFRA_FLAG_NAME)),
            ),
        )

        assert verdict.verdict.value == "MATCHED"
        assert verdict.leg1_declared_vs_declared.passed is True
        assert verdict.leg2_observed_producer_vs_declared.passed is False
        assert verdict.leg3_observed_consumer_vs_declared.passed is True
        assert verdict.regenerability.value == "SHAPE_ONLY"

    def test_a_renamed_literal_would_fail_the_registry_match(self) -> None:
        """Negative control: prove the match would actually catch the rename."""

        verdict = run_registry_match(
            edge_id="S13",
            declared_producer=producer_projection(
                edge_id="S13", topic="ONEX_MULTI_EVENT_PUBLISH_SEAM"
            ),
            declared_consumer=consumer_projection(
                edge_id="S13", topic="ONEX_MULTI_EVENT_PUBLISH_SEAM_V2"
            ),
        )

        assert verdict.verdict.value == "MISMATCH"
        assert verdict.leg1_declared_vs_declared.mismatching_field_path == "topic"
