# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical code-entity repository protocol (OMN-15230).

Before this module the name ``ProtocolCodeEntityRepository`` was declared
**twice** — once inside ``handler_code_embedding_effect`` (embedding half) and
once inside ``handler_code_enrichment_effect`` (enrichment half) — as two
independent, structurally different classes that happened to share a name.

Two facts make that arrangement worse than it looks:

1. ``ModelONEXContainer`` resolves services by ``protocol_type.__name__`` (see
   ``ContainerServiceRegistry.register_instance`` / ``get_service_async``: the
   interface map is keyed on the *string* ``interface.__name__``). Two distinct
   class objects sharing a name therefore collapse onto **one** registry slot —
   whichever provider registers last silently wins for *both* handlers, and a
   provider that only satisfies one half resolves cleanly for the other and then
   raises ``AttributeError`` mid-dispatch.
2. Each handler module owning its own copy meant any shared implementation had
   to import a node's private handler module, which the repo forbids ("Do not
   make one node import another node's private handler or model package.
   Promote shared types instead." — omnimarket CLAUDE.md).

So the two halves are unified here into one canonical protocol with the union of
both method sets, in a shared package. One name, one class object, one DI key,
one registration. Both handler modules import this symbol; neither declares its
own.

The method signatures are unchanged from the two originals (keyword-only
``limit``, keyword-only ``update_enrichment`` params), so every existing caller
and test double keeps working.

Implementations:
    - ``omnimarket.repositories.repository_code_entity_postgres.RepositoryCodeEntityPostgres``
      (production, Postgres-backed).

Consumers:
    - ``node_code_embedding_effect`` — ``get_entities_needing_embedding`` /
      ``update_embedded_at``.
    - ``node_code_enrichment_effect`` — ``get_entities_needing_enrichment`` /
      ``update_enrichment``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ProtocolCodeEntityRepository"]


@runtime_checkable
class ProtocolCodeEntityRepository(Protocol):
    """Read/update access to the code-entity store.

    A "code entity" is one row of the AST-extraction store: a named code
    construct (class / protocol / model / function) extracted from a source
    repository, keyed by ``(qualified_name, source_repo)``. The row carries
    source-derived fields (``entity_name``, ``entity_type``, ``qualified_name``,
    ``source_repo``, ``source_path``, ``signature``, ``docstring``, ``bases``,
    ``methods``, ``fields``, ``decorators``) plus derived enrichment fields
    (``classification``, ``llm_description``, ``architectural_pattern``,
    ``classification_confidence``, ``enrichment_version``) and freshness
    timestamps (``last_extracted_at``, ``last_enriched_at``,
    ``last_embedded_at``).

    Rows are returned as ``dict[str, Any]`` because the two consuming handlers
    read them by key (``entity["id"]``, ``entity.get("llm_description")``, ...)
    and neither declares a typed row model. That shape is inherited from the
    original protocol declarations, not introduced here.
    """

    async def get_entities_needing_embedding(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        """Return up to *limit* entities whose vector is missing or stale.

        "Stale" means the entity was re-extracted after it was last embedded.
        """
        ...

    async def update_embedded_at(self, entity_ids: list[str]) -> None:
        """Mark *entity_ids* as embedded as of now.

        An empty list is a no-op.
        """
        ...

    async def get_entities_needing_enrichment(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        """Return up to *limit* entities that have never been classified."""
        ...

    async def update_enrichment(
        self,
        *,
        entity_id: str,
        classification: str,
        llm_description: str,
        architectural_pattern: str,
        classification_confidence: float,
        enrichment_version: str,
    ) -> None:
        """Persist one entity's LLM enrichment result and stamp it enriched."""
        ...
