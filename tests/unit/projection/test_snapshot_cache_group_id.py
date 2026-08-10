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

    def test_two_instances_get_distinct_groups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SnapshotCache is a full-topic STATE cache, not a work queue --
        every replica must see every partition (preserved from the pre-fix
        module docstring). Two default-derived caches (two process replicas)
        must NOT share a group, or Kafka would split partitions between
        them."""
        monkeypatch.setenv("ONEX_ENVIRONMENT", "onex-dev")
        cache_a = _make_cache()
        cache_b = _make_cache()
        assert cache_a._group_id != cache_b._group_id
        # Both still derive from the same authorized base.
        assert _is_authorized(cache_a._group_id)
        assert _is_authorized(cache_b._group_id)

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
