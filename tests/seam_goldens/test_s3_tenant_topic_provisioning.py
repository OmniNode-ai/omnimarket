# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S3 — provisioned tenant topics vs the bare names the cloud actually uses.

The tenant topic provisioner creates ONLY tenant-prefixed wire topics
(``tenant-<slug>.`` + each canonical topic), while onex-api's own publisher and
terminal consumer both use the BARE names, which that provisioner never
creates. The registry classifies this UNMATCHED at high severity, making it
mandatory under the WS-7 union rule. It also underpins whether S1/S2's bare
topics can exist on the wire at all.

**Reachability limit, stated plainly.** The provisioner's own symbol
(``DEFAULT_TENANT_CANONICAL_TOPICS``) is NOT importable from this repo's
dependency closure — it is not present anywhere in the pinned
``omnibase_core`` / ``omnibase_infra`` / ``omnibase_spi`` / ``omnibase_compat``
packages. The frozen slice manifest records this as
``producer_symbol_reachable: false`` rather than burying it in a docstring, and
the edge is NOT claimed as traversed.

So this golden does what is honestly available: it computes the provisioned
name set from the REAL ``prefix_topic`` function the provisioner's naming rule
uses, over the REAL contract-declared canonical topics, and proves that set is
disjoint from the bare names the runtime legs actually use. Both inputs are
real; the un-driven part is the provisioner's own topic list, which no test in
this repo can reach. That is a weaker proof than the S1/S2/S4-S6 goldens and is
labelled as such — the manifest, not this module's optimism, is the record.
"""

from __future__ import annotations

import pytest
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    prefix_topic,
    validate_canonical_topic,
)

from tests.seam_goldens.harness import (
    GATEWAY_TENANT_SLUG,
    assert_registry_classification,
    consumer_projection,
    gateway_mirror_topics,
    run_registry_match,
)
from tests.seam_goldens.manifest import slice_edge

pytestmark = pytest.mark.unit

# The bare names the cloud publisher and terminal consumer genuinely use —
# the same strings S1 and S2 are goldened against.
_BARE_NAMES_IN_USE: tuple[str, ...] = (
    "onex.cmd.omnibase-infra.delegation-request.v1",
    "onex.evt.omnibase-infra.delegation-completed.v1",
    "onex.evt.omnibase-infra.delegation-failed.v1",
)


def _provisioned_names() -> frozenset[str]:
    """Every topic the provisioner would create, by its real naming rule.

    Built with the actual ``prefix_topic`` helper over the actual
    contract-declared canonical topics, so the naming rule is executed rather
    than restated. The canonical topic LIST is the contract's mirror set
    standing in for the provisioner's own constant, which is unreachable here.
    """

    mirror = gateway_mirror_topics()
    canonical = {*mirror.inbound, *mirror.outbound}
    return frozenset(
        prefix_topic(GATEWAY_TENANT_SLUG, topic) for topic in sorted(canonical)
    )


class TestReachabilityIsRecordedNotHidden:
    def test_slice_row_declares_the_producer_unreachable(self) -> None:
        edge = slice_edge("S3")
        assert edge.producer_symbol_reachable is False
        assert edge.traversed is False
        assert edge.registry_severity == "high"
        assert edge.registry_classification == "UNMATCHED"


class TestProvisionedNamesAreAllPrefixed:
    """The producer side's naming rule, executed against real code."""

    def test_every_provisioned_name_carries_the_tenant_prefix(self) -> None:
        for name in _provisioned_names():
            assert name.startswith(f"tenant-{GATEWAY_TENANT_SLUG}.")

    def test_the_naming_rule_refuses_to_emit_a_bare_name(self) -> None:
        """There is no input for which the provisioner yields a bare topic.

        ``prefix_topic`` unconditionally prepends the tenant segment and
        rejects an already-prefixed input, so no argument produces a bare
        name. That is what makes the disjointness below structural rather
        than incidental to the current topic list.
        """

        for bare in _BARE_NAMES_IN_USE:
            assert prefix_topic(GATEWAY_TENANT_SLUG, bare) != bare

        with pytest.raises(ValueError, match="must not carry tenant prefix"):
            prefix_topic(
                GATEWAY_TENANT_SLUG,
                f"tenant-{GATEWAY_TENANT_SLUG}.{_BARE_NAMES_IN_USE[0]}",
            )


class TestConsumerUsesBareNamesThatAreNeverCreated:
    """The consumer side, and the disjointness that is the defect."""

    @pytest.mark.parametrize("bare_name", _BARE_NAMES_IN_USE)
    def test_the_bare_name_is_a_valid_canonical_topic(self, bare_name: str) -> None:
        """Rules out "the bare name is malformed" as an alternative story."""

        assert validate_canonical_topic(bare_name) == bare_name

    @pytest.mark.parametrize("bare_name", _BARE_NAMES_IN_USE)
    def test_the_bare_name_is_never_provisioned(self, bare_name: str) -> None:
        assert bare_name not in _provisioned_names()

    def test_provisioned_and_in_use_name_sets_are_fully_disjoint(self) -> None:
        """The whole defect in one assertion, over real inputs on both sides."""

        assert _provisioned_names() & set(_BARE_NAMES_IN_USE) == set()


class TestS3RegistryMatch:
    def test_registry_match_reports_unmatched(self) -> None:
        """A consumed seam with no producer — the UNMATCHED direction.

        ``declared_producer=None`` is the faithful encoding: the provisioner
        creates no topic the consumer names. Inventing a producer projection
        here would manufacture agreement where the registry records none.
        """

        verdict = run_registry_match(
            edge_id="S3",
            declared_producer=None,
            declared_consumer=consumer_projection(
                edge_id="S3",
                topic=_BARE_NAMES_IN_USE[0],
                key_fields=(("topic_name", "str"),),
            ),
        )

        assert_registry_classification("S3", verdict)
        assert verdict.regenerability.value == "NOT_APPLICABLE"
        assert verdict.declared_producer_hash is None
