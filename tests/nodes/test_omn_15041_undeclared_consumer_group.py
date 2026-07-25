# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15041: consumer-group-vs-contract diff gate (reverse direction).

``_check_consumer_liveness`` (OMN-14528) asks "does every declared,
subscribing contract have a live group" — expected-but-absent, real wiring
death. This module proves the OPPOSITE direction: "does every live group
belong to a declared contract" — present-but-undeclared, an orphan or a
group minted outside ``compute_consumer_group_id()``.

The motivating bug (OMN-14843): the pre-fix ``CORE_RUNTIME_GROUP =
"onex.core-runtime.delegation"`` module-constant literal produced a live
consumer group with NO contract behind it. Because nothing tied it to a
contract, an operator reading the broker could not tell "expected kernel
wiring" from "orphaned casualty" — it read as "delegation is dead" and cost a
full investigation lane to disprove. This test suite recreates that exact
shape (a live group with no declaring contract) hermetically, against an
in-memory request — never a live broker/lane — and proves the gate:

1. Fails RED on an unaccounted-for group (the orphan scenario).
2. Passes GREEN once the group is accounted for (contract added, or a
   reasoned/ticketed grandfather entry matches).
3. Inherits the SAME fail-closed guarantees as OMN-14528: an unreachable
   broker (census=None, required) still raises; an empty/vacuous census
   still raises rather than reporting a false "no undeclared groups" clean
   pass.
4. The grandfather allowlist is a ratchet — bounded, enumerated, and its
   count only shrinks.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_COMPILED,
    _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_MAX_COUNT,
    _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_PATTERNS,
    EnumFindingType,
    EnumSweepCheck,
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)


def _known_contract(node_name: str) -> ModelContractInput:
    return ModelContractInput(
        node_name=node_name,
        description="A real, sufficiently long node description here.",
        subscribe_topics=["onex.cmd.demo.do.v1"],
        runtime_profiles=["effects"],
    )


@pytest.mark.unit
class TestUndeclaredConsumerGroupOrphanScenario:
    """RED-first: recreate the OMN-14843 orphan shape and prove the gate catches it."""

    def test_orphan_group_with_no_contract_is_flagged(self) -> None:
        """A live group matching NO declared node identity is UNDECLARED_CONSUMER_GROUP.

        This is the general form of the historical bug: the pre-fix
        ``CORE_RUNTIME_GROUP`` literal was ``"onex.core-runtime.delegation"``
        — a bare 3-segment string with no contract behind it and no
        recognizable purpose token. This test uses that EXACT literal as the
        orphan group to prove the fix would have caught the real production
        defect.
        """
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=[
                "dev.omnimarket.node_alpha_effect.consume.v1",
                "onex.core-runtime.delegation",  # the historical orphan literal
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)

        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 1
        finding = next(
            f
            for f in result.findings
            if f.finding_type == EnumFindingType.UNDECLARED_CONSUMER_GROUP
        )
        assert finding.subject == "onex.core-runtime.delegation"
        assert finding.severity == "CRITICAL"

    def test_orphan_resolved_once_contract_accounts_for_it(self) -> None:
        """GREEN: once a contract declares the node, the SAME group is no longer flagged."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                _known_contract("node_alpha_effect"),
                _known_contract("core-runtime"),
            ],
            live_consumer_groups=[
                "dev.omnimarket.node_alpha_effect.consume.v1",
                "onex.core-runtime.consume.v1",
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)

        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 0

    def test_orphan_resolved_by_grandfather_allowlist(self) -> None:
        """GREEN: a group matching a reasoned, ticketed allowlist entry is not flagged.

        Uses the live ``contract-registry`` kernel pattern as the concrete
        allowlisted case — a real, currently-grandfathered entry.
        """
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=[
                "dev.omnimarket.node_alpha_effect.consume.v1",
                "stability-test.runtime_config.contract-registry.contract-registry.1.0.0",
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)

        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 0

    def test_declared_but_unmatched_purpose_still_matches(self) -> None:
        """A declared node's group under a non-CONSUME purpose is still accounted for."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=[
                "dev.omnimarket.node_alpha_effect.introspection.v1",
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)

        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 0

    def test_multiple_orphans_all_flagged(self) -> None:
        """More than one unaccounted-for group each produce their own finding."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=[
                "dev.omnimarket.node_alpha_effect.consume.v1",
                "onex.mystery-service.ghost-consumer",
                "totally-unaccountable-group-name",
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)

        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 2
        subjects = {
            f.subject
            for f in result.findings
            if f.finding_type == EnumFindingType.UNDECLARED_CONSUMER_GROUP
        }
        assert subjects == {
            "onex.mystery-service.ghost-consumer",
            "totally-unaccountable-group-name",
        }


@pytest.mark.unit
class TestUndeclaredConsumerGroupFailClosed:
    """Inherits the OMN-14528 fail-closed guarantees for the reverse direction too."""

    def test_unreachable_broker_census_none_fails(self) -> None:
        """A required census that is None (broker never probed) raises — not a pass."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=None,
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        with pytest.raises(ValueError, match="census is None"):
            handler.handle(request)

    def test_empty_census_is_not_a_vacuous_pass(self) -> None:
        """An empty expected set matching an empty actual set must NOT read as success.

        A required census that scanned ZERO live groups fails closed rather
        than reporting "0 undeclared groups found" — that would be the exact
        green-over-nothing pattern this ticket exists to prevent.
        """
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_alpha_effect")],
            live_consumer_groups=[],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        with pytest.raises(ValueError, match="ZERO live consumer groups"):
            handler.handle(request)

    def test_unrelated_known_set_does_not_vacuously_exempt_other_groups(self) -> None:
        """A non-empty but unrelated known-node set does not exempt other groups.

        The reverse check's "declared" test is per-group, not a blanket pass
        once ANY contract exists — a live group must match a matching
        contract-declared identity, not merely coexist with unrelated ones.
        """
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[_known_contract("node_only_known_node")],
            live_consumer_groups=[
                "dev.omnimarket.node_only_known_node.consume.v1",
                "some-completely-unknown-group",
            ],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)
        assert result.by_type.get("UNDECLARED_CONSUMER_GROUP", 0) == 1


@pytest.mark.unit
class TestGrandfatherAllowlistRatchet:
    """The grandfather allowlist is bounded, reasoned, and can only shrink."""

    def test_ratchet_ceiling_not_exceeded(self) -> None:
        """Hardcoded literal ceiling: bumping requires a deliberate PR edit here.

        This equality (not just <=) forces any addition OR removal to be a
        conscious edit of this test file, mirroring the
        ``FROZEN_PROTOCOLS_MODELS_MAX``-style ratchets used elsewhere in this
        codebase (only Class-B removals move the count).
        """
        assert _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_MAX_COUNT == 10
        assert (
            len(_GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_PATTERNS)
            <= _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_MAX_COUNT
        )

    def test_every_entry_has_a_reason_and_ticket(self) -> None:
        """No open/unjustified allowlist entries — every one is reasoned + ticketed."""
        for entry in _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_PATTERNS:
            assert entry.reason.strip(), f"pattern {entry.pattern!r} has no reason"
            assert re.match(r"^OMN-\d+$", entry.ticket), (
                f"pattern {entry.pattern!r} has no valid ticket ({entry.ticket!r})"
            )

    def test_every_pattern_compiles(self) -> None:
        """Every declared pattern is a valid, precompiled regex (no runtime surprise)."""
        assert len(_GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_COMPILED) == len(
            _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_PATTERNS
        )

    def test_patterns_are_narrow_not_universal_wildcards(self) -> None:
        """No entry is a bare ``.*``/blanket wildcard that would swallow everything."""
        for entry in _GRANDFATHERED_UNDECLARED_CONSUMER_GROUP_PATTERNS:
            assert entry.pattern not in {".*", "", ".+"}
            compiled = re.compile(entry.pattern)
            # A truly unconstrained pattern would match an unrelated random
            # string; none of the enumerated entries should.
            assert not compiled.search("totally-unrelated-random-group-xyz123")
