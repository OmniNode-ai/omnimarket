# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory doubles of the three seams the tenant topic provision handler is given."""

from __future__ import annotations

from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicState,
)

#: A synthetic mirror set the handler tests inject; the forwarder-contract tests read the real one.
_SYNTHETIC = ("alpha", "beta")
NAMES = tuple(
    f"onex.{kind}.synthetic.{name}.v1"
    for kind, name in zip(("cmd", "evt"), _SYNTHETIC, strict=True)
)
TEMPLATE = "tenant-{slug}.{topic}"


class FakeMirrorSource:
    def __init__(self, names: tuple[str, ...] = NAMES) -> None:
        self._names = names

    def mirror_topic_names(self) -> tuple[str, ...]:
        return self._names


class FakeBroker:
    """Lists what it holds and records every create and every delete call."""

    def __init__(
        self,
        live: tuple[str, ...] = (),
        *,
        refuse: frozenset[str] = frozenset(),
        list_fails: bool = False,
    ) -> None:
        self.live = list(live)
        self.refuse = set(refuse)
        self.list_fails = list_fails
        self.creates: list[tuple[str, int, int]] = []
        self.deletes: list[str] = []

    async def list_topics(self) -> tuple[str, ...]:
        if self.list_fails:
            raise ConnectionError("broker down")
        return tuple(sorted(self.live))

    async def create_topic(
        self, name: str, num_partitions: int, replication_factor: int
    ) -> bool:
        self.creates.append((name, num_partitions, replication_factor))
        if name in self.refuse:
            raise PermissionError(f"create {name}: not authorized")
        if name in self.live:
            return False
        self.live.append(name)
        return True


class FakeRegistry:
    """The tenants the registry holds and the topic state it was told for each."""

    def __init__(self, slugs: tuple[str, ...]) -> None:
        self.slugs = slugs
        self.states: dict[str, EnumTenantTopicState] = {}
        self.failed: dict[str, tuple[str, ...]] = {}

    async def list_tenant_slugs(self) -> tuple[str, ...]:
        return self.slugs

    async def record_state(
        self,
        tenant_slug: str,
        state: EnumTenantTopicState,
        failed_topics: tuple[str, ...],
    ) -> None:
        self.states[tenant_slug] = state
        self.failed[tenant_slug] = failed_topics


def wire(slug: str, names: tuple[str, ...] = NAMES) -> tuple[str, ...]:
    return tuple(TEMPLATE.format(slug=slug, topic=name) for name in names)
