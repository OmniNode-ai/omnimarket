# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public-consumer check: acceptance criteria, golden and error chains, injected breaks (OMN-23145)."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_checkout_scan import (
    SourceCheckoutScan,
)
from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_public_consumer_check_compute.handlers.handler_public_consumer_check import (
    HandlerPublicConsumerCheck,
)
from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    EnumBatchDecision,
    EnumEntityDisposition,
    EnumRarePathCategory,
    EnumScopeKind,
    ModelConsumerResolution,
    ModelEntity,
    ModelPublicConsumerCheckRequest,
    ModelRarePathDeclaration,
    ModelSearchScope,
)
from omnimarket.nodes.node_public_consumer_check_compute.overlay import (
    ModelPublicConsumerCheckOverlay,
    build_handler,
)

pytestmark = pytest.mark.unit

REPO = ModelSearchScope(kind=EnumScopeKind.PUBLIC_REPOSITORY, name="pub-repo")
PACKAGE = ModelSearchScope(kind=EnumScopeKind.PUBLISHED_PACKAGE, name="pub-package")
OWN = ModelSearchScope(kind=EnumScopeKind.PUBLIC_REPOSITORY, name="origin-repo")
SCOPES = (REPO, PACKAGE)
REPO_KEY = (REPO.kind.value, REPO.name)
PACKAGE_KEY = (PACKAGE.kind.value, PACKAGE.name)


def _source(
    repo_files: dict[str, str] | None = None,
    package_files: dict[str, str] | None = None,
    unreadable: frozenset[tuple[str, str]] = frozenset(),
) -> SourceInMemory:
    return SourceInMemory(
        {REPO_KEY: repo_files or {}, PACKAGE_KEY: package_files or {}}, unreadable
    )


def _request(
    *entities: ModelEntity,
    resolutions: tuple[ModelConsumerResolution, ...] = (),
) -> ModelPublicConsumerCheckRequest:
    return ModelPublicConsumerCheckRequest(
        batch_id="batch-1",
        repository="origin-repo",
        entities=entities
        or (ModelEntity(entity_id="e1", references=("pkg.old_mod",)),),
        resolutions=resolutions,
    )


def test_records_evidence_from_repository_and_package() -> None:
    source = _source(
        repo_files={"src/a.py": "x = 1\nfrom pkg.old_mod import thing\n"},
        package_files={"pkg/__init__.py": "import pkg.old_mod\n"},
    )
    result = HandlerPublicConsumerCheck(source, SCOPES).handle(_request())

    evidence = result.verdicts[0].evidence
    assert [(r.scope.name, r.read, r.hit_count) for r in evidence.scopes] == [
        ("pub-repo", True, 1),
        ("pub-package", True, 1),
    ]
    assert {
        (h.scope.name, h.locator, h.line, h.matched_reference) for h in evidence.hits
    } == {
        ("pub-repo", "src/a.py", 2, "pkg.old_mod"),
        ("pub-package", "pkg/__init__.py", 1, "pkg.old_mod"),
    }


def test_records_evidence_of_a_clean_search_names_every_scope_read() -> None:
    result = HandlerPublicConsumerCheck(_source(), SCOPES).handle(_request())

    verdict = result.verdicts[0]
    assert verdict.disposition is EnumEntityDisposition.CLEARED
    assert [(r.scope.name, r.read, r.hit_count) for r in verdict.evidence.scopes] == [
        ("pub-repo", True, 0),
        ("pub-package", True, 0),
    ]
    assert result.decision is EnumBatchDecision.ACCEPTED


def test_records_evidence_matches_whole_names_only() -> None:
    source = _source(
        repo_files={"a.py": "import pkg.old_module_extra\nimport other.pkg.old_mod\n"}
    )
    result = HandlerPublicConsumerCheck(source, SCOPES).handle(_request())
    assert result.verdicts[0].evidence.hits == ()


def test_unresolved_consumer_keeps_entity_and_refuses_the_batch() -> None:
    source = _source(repo_files={"src/a.py": "from pkg.old_mod import thing\n"})
    result = HandlerPublicConsumerCheck(source, SCOPES).handle(
        _request(
            ModelEntity(entity_id="used", references=("pkg.old_mod",)),
            ModelEntity(entity_id="unused", references=("pkg.never_imported",)),
        )
    )

    by_id = {v.entity_id: v for v in result.verdicts}
    assert by_id["used"].disposition is EnumEntityDisposition.KEPT_UNRESOLVED_CONSUMER
    assert by_id["used"].kept is True
    assert by_id["unused"].disposition is EnumEntityDisposition.CLEARED
    assert result.decision is EnumBatchDecision.REFUSED
    assert result.kept_entity_ids == ("used",)


def test_unresolved_consumer_keeps_entity_until_a_resolution_is_recorded() -> None:
    source = _source(repo_files={"src/a.py": "from pkg.old_mod import thing\n"})
    handler = HandlerPublicConsumerCheck(source, SCOPES)
    resolved = handler.handle(
        _request(
            resolutions=(
                ModelConsumerResolution(
                    scope=REPO, locator="src/a.py", resolution_ref="pull-request-merged"
                ),
            )
        )
    )
    assert resolved.verdicts[0].disposition is EnumEntityDisposition.CLEARED
    assert resolved.verdicts[0].evidence.hits[0].resolution_ref == "pull-request-merged"

    # A resolution for another place, or in another scope, resolves nothing.
    elsewhere = handler.handle(
        _request(
            resolutions=(
                ModelConsumerResolution(
                    scope=PACKAGE, locator="src/a.py", resolution_ref="x"
                ),
                ModelConsumerResolution(
                    scope=REPO, locator="src/b.py", resolution_ref="x"
                ),
            )
        )
    )
    assert elsewhere.decision is EnumBatchDecision.REFUSED


def test_unresolved_consumer_keeps_entity_when_a_scope_is_unreadable() -> None:
    result = HandlerPublicConsumerCheck(
        _source(unreadable=frozenset({PACKAGE_KEY})), SCOPES
    ).handle(_request())

    verdict = result.verdicts[0]
    assert verdict.disposition is EnumEntityDisposition.KEPT_SEARCH_UNREAD
    assert [r.read for r in verdict.evidence.scopes] == [True, False]
    assert result.decision is EnumBatchDecision.REFUSED


def test_unresolved_consumer_keeps_entity_with_no_scopes_or_no_source() -> None:
    assert (
        HandlerPublicConsumerCheck().handle(_request()).decision
        is EnumBatchDecision.REFUSED
    )
    no_source = HandlerPublicConsumerCheck(None, SCOPES).handle(_request())
    assert no_source.verdicts[0].disposition is EnumEntityDisposition.KEPT_SEARCH_UNREAD


def test_unresolved_consumer_keeps_entity_hit_in_own_repository_is_not_a_public_consumer() -> (
    None
):
    own_key = (OWN.kind.value, OWN.name)
    source = SourceInMemory({own_key: {"src/a.py": "import pkg.old_mod\n"}})
    result = HandlerPublicConsumerCheck(source, (OWN,)).handle(_request())
    assert result.verdicts[0].disposition is EnumEntityDisposition.CLEARED
    assert result.verdicts[0].evidence.hits[0].own_repository is True


def test_rare_path_declaration_naming_a_drill_keeps_the_entity() -> None:
    entity = ModelEntity(
        entity_id="restore",
        references=("pkg.restore",),
        rare_path=ModelRarePathDeclaration(
            category=EnumRarePathCategory.RECOVERY, drill="restore-drill-nightly"
        ),
    )
    result = HandlerPublicConsumerCheck(_source(), SCOPES).handle(_request(entity))

    verdict = result.verdicts[0]
    assert verdict.disposition is EnumEntityDisposition.KEPT_RARE_PATH
    assert verdict.kept is True
    assert "restore-drill-nightly" in verdict.reasons[0]
    assert result.decision is EnumBatchDecision.REFUSED
    # Its evidence is still recorded.
    assert len(verdict.evidence.scopes) == 2


@pytest.mark.parametrize("drill", ["", "   "])
def test_rare_path_declaration_naming_no_drill_is_not_exempt(drill: str) -> None:
    entity = ModelEntity(
        entity_id="rollback",
        references=("pkg.rollback",),
        rare_path=ModelRarePathDeclaration(
            category=EnumRarePathCategory.ROLLBACK, drill=drill
        ),
    )
    clean = HandlerPublicConsumerCheck(_source(), SCOPES).handle(_request(entity))
    assert clean.verdicts[0].disposition is EnumEntityDisposition.CLEARED
    assert clean.decision is EnumBatchDecision.ACCEPTED
    assert "names no drill: not exempt" in clean.verdicts[0].reasons[0]

    # Not exempt means it is judged like any entity: a public consumer still keeps it.
    consumed = HandlerPublicConsumerCheck(
        _source(repo_files={"a.py": "import pkg.rollback\n"}), SCOPES
    ).handle(_request(entity))
    assert (
        consumed.verdicts[0].disposition
        is EnumEntityDisposition.KEPT_UNRESOLVED_CONSUMER
    )


def test_rare_path_declaration_is_validated() -> None:
    with pytest.raises(ValidationError):
        ModelRarePathDeclaration(category="not-a-category", drill="d")
    with pytest.raises(ValidationError):
        ModelEntity(entity_id="e", references=(" ",))
    with pytest.raises(ValidationError):
        _request(
            ModelEntity(entity_id="e", references=("a",)),
            ModelEntity(entity_id="e", references=("b",)),
        )


def test_injected_break_a_blind_source_cannot_clear_an_entity() -> None:
    """Named break: the source reports no hits because it cannot read the scopes.

    The check must go red (the batch is refused), not read the silence as clear.
    """
    blind = SourceInMemory({}, frozenset())
    result = HandlerPublicConsumerCheck(blind, SCOPES).handle(_request())
    assert result.decision is EnumBatchDecision.REFUSED
    assert result.verdicts[0].disposition is EnumEntityDisposition.KEPT_SEARCH_UNREAD


def test_checkout_scan_source_reads_a_real_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "a.py").write_text("import pkg.old_mod\n", encoding="utf-8")
    (repo_root / ".git").mkdir()
    (repo_root / ".git" / "x").write_text("pkg.old_mod\n", encoding="utf-8")
    (repo_root / "big.txt").write_text("pkg.old_mod\n" * 100, encoding="utf-8")
    (repo_root / "bin.dat").write_bytes(b"\xff\xfe\x00pkg.old_mod")
    pkg_root = tmp_path / "pkg"
    pkg_root.mkdir()
    source = SourceCheckoutScan(
        {REPO_KEY: repo_root, PACKAGE_KEY: pkg_root},
        max_file_bytes=200,
        skip_dirs=frozenset({".git"}),
    )
    result = HandlerPublicConsumerCheck(source, SCOPES).handle(_request())
    assert [(h.scope.name, h.locator) for h in result.verdicts[0].evidence.hits] == [
        ("pub-repo", "src/a.py")
    ]

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    missing = SourceCheckoutScan({REPO_KEY: clean_root}, 200, frozenset())
    unread = HandlerPublicConsumerCheck(missing, SCOPES).handle(_request())
    assert unread.verdicts[0].disposition is EnumEntityDisposition.KEPT_SEARCH_UNREAD


def test_overlay_example_builds_a_handler_and_unset_root_reads_unreadable(
    tmp_path: Path,
) -> None:
    text = (
        files("omnimarket.nodes.node_public_consumer_check_compute")
        .joinpath("overlay.example.yaml")
        .read_text()
    )
    document: dict[str, Any] = yaml.safe_load(text)
    overlay = ModelPublicConsumerCheckOverlay.model_validate(document)
    assert overlay.source_backend == "in_memory"

    scanned = overlay.model_copy(update={"source_backend": "checkout_scan"})
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.py").write_text("import pkg.old_mod\n", encoding="utf-8")
    handler = build_handler(scanned, {"EXAMPLE_PUBLIC_REPO_ROOT": str(root)})
    result = handler.handle(_request())
    names = {(r.scope.name, r.read) for r in result.verdicts[0].evidence.scopes}
    assert names == {
        ("example-public-repo", True),
        ("example-published-package", False),
    }
    assert result.decision is EnumBatchDecision.REFUSED

    with pytest.raises(ValidationError):
        ModelPublicConsumerCheckOverlay.model_validate(
            {**document, "scopes": [*document["scopes"], document["scopes"][0]]}
        )
    with pytest.raises(ValidationError):
        ModelPublicConsumerCheckOverlay.model_validate(
            {k: v for k, v in document.items() if k != "max_file_bytes"}
        )
