# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15840 -- SnapshotCache's default consumer group id must be canonically
derived (not a bespoke ``f"{prefix}-{uuid4()}"`` literal) so it lands inside
the six MSK-IAM-pinned consumer-group patterns on onex-dev.

Root cause (live, deploy run 31412376323, 2026-08-10T17:22:39Z): the pre-fix
literal ``omnimarket-projection-api-snapshot-cache-v1-<uuid4>`` matches none
of the six patterns pinned in
``omninode_infra/tests/test_msk_group_pattern_pin.py:139-144``
(``onex-dev.*``, ``local.runtime_config.*``, ``pattern-b-broker-*``,
``onex.*``, ``omninode.*``, ``phase5-msk-smoke-*``) -- the consumer died
``GroupAuthorizationFailedError`` before it could join.

Reference fix (same class, OMN-15700): ``omnibase_infra#2681`` (merged
``d098bc03e``) replaced a hand-rolled ``f"savings-estimator.{topic}"``
literal with ``ModelNodeIdentity`` + ``compute_consumer_group_id`` -- the
canonical, environment-qualified derivation authority. This fix reuses that
exact mechanism rather than inventing a parallel one.

RED before the fix (recorded 2026-08-10): ``SnapshotCache`` has no canonical
default-derivation helper; the constructor's fallback is
``f"{DEFAULT_GROUP_ID_PREFIX}-{uuid.uuid4()}"``, which never starts with
``onex-dev.`` (or any of the other five prefixes) no matter what
``ONEX_ENVIRONMENT`` is set to -- every parametrized case below fails against
pre-fix code, and the unset-environment case does not raise at all (pre-fix
code never reads ``ONEX_ENVIRONMENT``).
"""

from __future__ import annotations

import re

import pytest

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.test-group-id.v1"
_BESPOKE_LITERAL_PREFIX = "omnimarket-projection-api-snapshot-cache-v1-"
#: OMN-15904: the per-process uuid4 suffix the group id used to end in.
_UUID4_SUFFIX = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# Vendored copy of the six MSK IAM consumer-group resource patterns pinned in
# omninode_infra/tests/test_msk_group_pattern_pin.py:139-144 (Terraform
# source: aws/cluster-dev/managed-data-plane.auto.tfvars). MSK IAM resource
# patterns use whole-name glob semantics: '*' is the only wildcard and '.' is
# a LITERAL character -- a substring test would wrongly accept a name that
# merely CONTAINS "onex-dev." without starting with it.
_PINNED_GROUP_PATTERNS: tuple[str, ...] = (
    "onex-dev.*",
    "local.runtime_config.*",
    "pattern-b-broker-*",
    "onex.*",
    "omninode.*",
    "phase5-msk-smoke-*",
)


def _compile_iam_glob(pattern: str) -> re.Pattern[str]:
    compiled = "".join(".*" if char == "*" else re.escape(char) for char in pattern)
    return re.compile(f"^{compiled}\\Z")


def _is_authorized(group_name: str) -> bool:
    return any(
        _compile_iam_glob(pattern).match(group_name) is not None
        for pattern in _PINNED_GROUP_PATTERNS
    )


def _exposure() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_TOPIC,
        table="test_table",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )


def _make_cache() -> SnapshotCache:
    return SnapshotCache({_TOPIC: _exposure()}, bootstrap_servers="unused:9092")


class TestDefaultGroupIdIsCanonicallyDerived:
    """No explicit ``group_id`` override -> canonical derivation, never the
    bespoke ``{prefix}-{uuid4()}`` literal."""

    def test_bespoke_literal_prefix_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves this is a genuine re-derivation, not the old literal with
        the env var merely consulted for show."""
        monkeypatch.setenv("ONEX_ENVIRONMENT", "onex-dev")
        cache = _make_cache()
        assert _BESPOKE_LITERAL_PREFIX not in cache._group_id

    @pytest.mark.parametrize("environment", ["onex-dev"])
    def test_derived_group_id_is_authorized(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        """The ONEX_ENVIRONMENT value the onex-dev ConfigMap actually sets
        must produce a group id one of the six pinned MSK IAM patterns
        authorizes."""
        monkeypatch.setenv("ONEX_ENVIRONMENT", environment)
        cache = _make_cache()
        assert _is_authorized(cache._group_id), (
            f"{cache._group_id!r} matches none of {_PINNED_GROUP_PATTERNS!r}"
        )

    def test_unset_environment_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15835 flags a fail-open 'local' default as its own bug class
        (same-shape defect on the savings-estimator group). This cache must
        never repeat it: a missing ONEX_ENVIRONMENT raises instead of
        silently defaulting."""
        monkeypatch.delenv("ONEX_ENVIRONMENT", raising=False)
        with pytest.raises(KeyError):
            _make_cache()

    def test_two_instances_get_the_same_stable_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REPLACES test_two_instances_get_distinct_groups (OMN-15904).

        The invariant that test protected is unchanged and still enforced:
        SnapshotCache is a full-topic STATE cache, not a work queue, so every
        replica must see every partition. What changed is WHICH mechanism holds
        it. It used to be group-id uniqueness -- give each replica its own
        group and Kafka's coordinator cannot split partitions between them.
        That made the id unresumable by construction, because a group nobody has
        committed to has no committed offset, and every restart replayed the
        topic from offset 0 (measured: 903s at 12:55Z, over 1800s by 19:55Z on
        2026-09-26, killing thirteen consecutive staging deploys).

        The invariant now lives in ``consumer.assign()``, which never consults
        the coordinator, so coverage cannot depend on the id being unique --
        which frees the id to be stable, which is what lets a restart resume.
        The coverage half is asserted directly in
        test_snapshot_cache_stable_group_resume_omn15904.py; this case asserts
        the id half, and that dropping the discriminator did not move the id
        outside the authorized patterns.
        """
        monkeypatch.setenv("ONEX_ENVIRONMENT", "onex-dev")
        cache_a = _make_cache()
        cache_b = _make_cache()
        assert cache_a._group_id == cache_b._group_id, (
            "two replicas must share one group id, or neither can resume from "
            "the other's committed offsets after a restart (OMN-15904)"
        )
        assert _is_authorized(cache_a._group_id)
        assert _is_authorized(cache_b._group_id)
        assert "-" in cache_a._group_id
        # The discriminator was a uuid4 suffix; assert it is gone rather than
        # merely that two ids match, which an accidental constant would satisfy.
        assert not _UUID4_SUFFIX.search(cache_a._group_id), (
            f"group id still carries a per-process suffix: {cache_a._group_id}"
        )

    def test_explicit_override_still_wins(self) -> None:
        """A caller-supplied group_id (existing tests, or any future explicit
        override) is used verbatim -- the canonical derivation is a DEFAULT,
        not mandatory, and must not consult ONEX_ENVIRONMENT at all when an
        explicit id is given."""
        cache = SnapshotCache(
            {_TOPIC: _exposure()},
            bootstrap_servers="unused:9092",
            group_id="explicit-test-group",
        )
        assert cache._group_id == "explicit-test-group"
