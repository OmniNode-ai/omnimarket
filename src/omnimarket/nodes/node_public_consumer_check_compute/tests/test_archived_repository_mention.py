# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A public mention of an archived repository keeps its entities, and the batch records why (OMN-23146 AC2)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_public_consumer_check_compute.handlers.handler_public_consumer_check import (
    HandlerPublicConsumerCheck,
)
from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    EnumBatchDecision,
    EnumEntityDisposition,
    EnumScopeKind,
    ModelConsumerResolution,
    ModelEntity,
    ModelPublicConsumerCheckRequest,
    ModelSearchScope,
)

pytestmark = pytest.mark.unit

ARCHIVED = "archived-repo"
PUBLIC = ModelSearchScope(kind=EnumScopeKind.PUBLIC_REPOSITORY, name="pub-repo")
OTHER = ModelSearchScope(kind=EnumScopeKind.PUBLISHED_PACKAGE, name="pub-package")
OWN = ModelSearchScope(kind=EnumScopeKind.PUBLIC_REPOSITORY, name=ARCHIVED)
PUBLIC_KEY = (PUBLIC.kind.value, PUBLIC.name)
OTHER_KEY = (OTHER.kind.value, OTHER.name)
OWN_KEY = (OWN.kind.value, OWN.name)
MENTION = "docs/README.md"


def _request(
    *,
    names: tuple[str, ...] = (ARCHIVED,),
    resolutions: tuple[ModelConsumerResolution, ...] = (),
) -> ModelPublicConsumerCheckRequest:
    return ModelPublicConsumerCheckRequest(
        batch_id="stage-1-archived-repo",
        repository=ARCHIVED,
        entities=(
            ModelEntity(entity_id="e1", references=("old_pkg.alpha",)),
            ModelEntity(entity_id="e2", references=("old_pkg.beta",)),
        ),
        resolutions=resolutions,
        archived_repository_names=names,
    )


def _source(
    public: dict[str, str] | None = None,
    other: dict[str, str] | None = None,
    unreadable: frozenset[tuple[str, str]] = frozenset(),
) -> SourceInMemory:
    return SourceInMemory(
        {
            PUBLIC_KEY: public or {},
            OTHER_KEY: other or {},
            OWN_KEY: {"README.md": f"# {ARCHIVED}\n"},
        },
        unreadable,
    )


def test_archived_repository_mention_keeps_every_entity_and_records_why() -> None:
    source = _source(public={MENTION: f"see {ARCHIVED} for history\n"})
    result = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER)).handle(_request())

    # Neither entity is imported anywhere: only the repository's name is mentioned, and that alone keeps both.
    assert result.decision is EnumBatchDecision.REFUSED
    assert result.kept_entity_ids == ("e1", "e2")
    assert {v.disposition for v in result.verdicts} == {
        EnumEntityDisposition.KEPT_ARCHIVED_REPOSITORY_MENTION
    }
    assert all(v.evidence.hits == () for v in result.verdicts)
    [mention] = result.archived_mentions
    assert (
        mention.scope.name,
        mention.locator,
        mention.line,
        mention.matched_name,
    ) == (
        "pub-repo",
        MENTION,
        1,
        ARCHIVED,
    )
    assert mention.resolved is False
    # The batch records why, once at the batch and once on each entity.
    assert result.reasons == (
        f"archived repository {ARCHIVED} is mentioned in public_repository:pub-repo:{MENTION}:1 "
        f"as {ARCHIVED}: its entities stay until the mention is resolved",
    )
    assert all(v.reasons[0] == result.reasons[0] for v in result.verdicts)


def test_archived_repository_mention_in_a_published_package_keeps_the_entities() -> (
    None
):
    source = _source(other={"pkg/meta.txt": f"homepage: {ARCHIVED}\n"})
    result = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER)).handle(_request())
    assert result.decision is EnumBatchDecision.REFUSED
    assert [m.scope.name for m in result.archived_mentions] == ["pub-package"]


def test_archived_repository_mention_is_cleared_by_a_recorded_resolution() -> None:
    source = _source(public={MENTION: f"see {ARCHIVED} for history\n"})
    handler = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER))
    resolved = handler.handle(
        _request(
            resolutions=(
                ModelConsumerResolution(
                    scope=PUBLIC, locator=MENTION, resolution_ref="link-removed"
                ),
            )
        )
    )
    assert resolved.decision is EnumBatchDecision.ACCEPTED
    assert resolved.reasons == ()
    assert resolved.archived_mentions[0].resolution_ref == "link-removed"
    assert {v.disposition for v in resolved.verdicts} == {EnumEntityDisposition.CLEARED}

    # A resolution for another place resolves nothing.
    elsewhere = handler.handle(
        _request(
            resolutions=(
                ModelConsumerResolution(
                    scope=PUBLIC, locator="other.md", resolution_ref="x"
                ),
            )
        )
    )
    assert elsewhere.decision is EnumBatchDecision.REFUSED


def test_archived_repository_mention_in_its_own_repository_is_not_public() -> None:
    result = HandlerPublicConsumerCheck(_source(), (OWN, PUBLIC)).handle(_request())
    assert result.archived_mentions == ()
    assert result.decision is EnumBatchDecision.ACCEPTED


def test_archived_repository_mention_search_runs_only_when_names_are_declared() -> None:
    source = _source(public={MENTION: f"see {ARCHIVED} for history\n"})
    result = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER)).handle(
        _request(names=())
    )
    assert result.archived_mentions == ()
    assert result.decision is EnumBatchDecision.ACCEPTED


def test_archived_repository_mention_matches_whole_names_only() -> None:
    source = _source(public={MENTION: f"see not{ARCHIVED}x and my.{ARCHIVED}\n"})
    result = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER)).handle(_request())
    assert result.archived_mentions == ()


def test_archived_repository_mention_unread_scope_keeps_the_entities() -> None:
    source = _source(unreadable=frozenset({OTHER_KEY}))
    result = HandlerPublicConsumerCheck(source, (PUBLIC, OTHER)).handle(_request())
    assert result.decision is EnumBatchDecision.REFUSED
    assert any("unread" in reason for reason in result.reasons)


def test_archived_repository_mention_with_no_source_keeps_the_entities() -> None:
    result = HandlerPublicConsumerCheck(None, (PUBLIC,)).handle(_request())
    assert result.decision is EnumBatchDecision.REFUSED
    assert result.reasons


def test_archived_repository_mention_blank_name_is_refused() -> None:
    with pytest.raises(ValueError, match="blank"):
        _request(names=(" ",))


def test_injected_break_archived_repository_mention_must_not_read_silence_as_clear() -> (
    None
):
    """Positive control: the same source with the repository's name removed from it clears the batch."""
    clean = HandlerPublicConsumerCheck(_source(), (PUBLIC, OTHER)).handle(_request())
    assert clean.decision is EnumBatchDecision.ACCEPTED
    assert clean.archived_mentions == ()
