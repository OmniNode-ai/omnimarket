# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage 2 of unused-code removal: keep the copy dev loads of each duplicated node contract name, remove the others.

For each contract name found in more than one repository the loaded copy is read first. The copy dev loads is kept and
is never handed to the quarantine handler; every other copy goes through it with its tests (quarantine first, then a
removal with a tag and a catalog row, one repository per batch). A loaded copy that cannot be read, or whose files
cannot be seen, keeps every copy of the name and the group is reported once as unread; a clean reading that names none
of the copies keeps them all too. Nothing here deletes or excludes a path itself.
"""

from __future__ import annotations

from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    KEPT_DISPOSITIONS,
    EnumQuarantineMode,
    EnumStage2Disposition,
    EnumStage2Mode,
    ModelCatalogRow,
    ModelDuplicateCopy,
    ModelDuplicateGroup,
    ModelQuarantineRequest,
    ModelRemovalBatch,
    ModelStage2CopyOutcome,
    ModelStage2Event,
    ModelStage2GroupResult,
    ModelStage2Request,
    ModelStage2Result,
    one_line,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import (
    LoadedCopyUnreadableError,
    ProtocolLoadedCopyReader,
    ProtocolRepository,
    ProtocolStage2Reporter,
    QuarantineStepError,
)


class HandlerStage2Duplicates:
    def __init__(
        self,
        *,
        quarantine: HandlerCodeQuarantine,
        loaded: ProtocolLoadedCopyReader,
        repository: ProtocolRepository,
        reporter: ProtocolStage2Reporter,
    ) -> None:
        self._quarantine = quarantine
        self._loaded = loaded
        self._repo = repository
        self._reporter = reporter
        self._calls = 0

    def handle(self, request: ModelStage2Request) -> ModelStage2Result:
        results: list[ModelStage2GroupResult] = []
        removals: dict[str, list[tuple[ModelDuplicateGroup, str]]] = {}
        for group in request.groups:
            result, spare = self._read_group(request, group)
            results.append(result)
            if request.mode is EnumStage2Mode.REMOVE:
                for repository in spare:
                    removals.setdefault(repository, []).append(
                        (group, result.loaded_repository)
                    )
        if request.mode is EnumStage2Mode.REMOVE:
            results = self._remove(request, results, removals)
        for group_result in results:
            if group_result.disposition in KEPT_DISPOSITIONS:
                self._report(request, group_result)
        return ModelStage2Result(
            run_id=request.run_id, mode=request.mode, groups=tuple(results)
        )

    def _report(
        self, request: ModelStage2Request, result: ModelStage2GroupResult
    ) -> None:
        self._reporter.report(
            ModelStage2Event(
                correlation_id=request.run_id,
                kind=f"code-quarantine.stage-2.{result.disposition.value}",
                contract_name=result.contract_name,
                disposition=result.disposition,
                detail=result.detail,
                reading="NOT WORKING"
                if result.disposition is EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD
                else "WORKING",
            )
        )

    def _next_run_id(self, request: ModelStage2Request) -> str:
        self._calls += 1
        return f"{request.run_id}-s2-{self._calls}"

    def _kept_whole(
        self,
        group: ModelDuplicateGroup,
        disposition: EnumStage2Disposition,
        detail: str,
        loaded: str = "",
    ) -> ModelStage2GroupResult:
        return ModelStage2GroupResult(
            contract_name=group.contract_name,
            disposition=disposition,
            loaded_repository=loaded,
            detail=detail,
            copies=tuple(
                ModelStage2CopyOutcome(repository=c.repository, kept=True)
                for c in group.copies
            ),
        )

    def _read_group(
        self, request: ModelStage2Request, group: ModelDuplicateGroup
    ) -> tuple[ModelStage2GroupResult, tuple[str, ...]]:
        """Read the loaded copy; keep the group whole unless it is read, present and one of the copies."""
        if len(group.copies) < 2:
            return (
                self._kept_whole(
                    group,
                    EnumStage2Disposition.KEPT_NOT_DUPLICATED,
                    f"{group.contract_name} has {len(group.copies)} copy named; nothing is a duplicate",
                ),
                (),
            )
        try:
            loaded = self._loaded.loaded_repository(group.contract_name)
        except LoadedCopyUnreadableError as exc:
            return (
                self._kept_whole(
                    group,
                    EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD,
                    f"the loaded copy of {group.contract_name} is unread ({one_line(str(exc))}); every copy stays with its tests",
                ),
                (),
            )
        keep = next((c for c in group.copies if c.repository == loaded), None)
        if keep is None:
            return (
                self._kept_whole(
                    group,
                    EnumStage2Disposition.KEPT_NO_LOADED_COPY,
                    f"dev loads {loaded or 'none'} of {group.contract_name}, not one of "
                    f"{', '.join(c.repository for c in group.copies)}; every copy stays with its tests",
                ),
                (),
            )
        missing = [
            p for p in keep.entity_paths if not self._try_exists(keep.repository, p)
        ]
        if missing:
            return (
                self._kept_whole(
                    group,
                    EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD,
                    f"the loaded copy of {group.contract_name} in {loaded} cannot be read at "
                    f"{', '.join(missing)}; every copy stays with its tests",
                    loaded,
                ),
                (),
            )
        spare = tuple(c.repository for c in group.copies if c is not keep)
        copies = [
            ModelStage2CopyOutcome(repository=c.repository, kept=c is keep)
            for c in group.copies
        ]
        result = ModelStage2GroupResult(
            contract_name=group.contract_name,
            disposition=EnumStage2Disposition.LOADED_COPY_KEPT,
            loaded_repository=loaded,
            detail=f"dev loads the copy in {loaded}; it stays; {len(spare)} other copy(ies) with their tests go",
            copies=tuple(copies),
        )
        if request.mode is EnumStage2Mode.QUARANTINE:
            result = self._quarantine_spare(request, group, result, keep)
        return result, spare

    def _try_exists(self, repository: str, path: str) -> bool:
        try:
            return self._repo.path_exists(repository, path)
        except QuarantineStepError:
            return False

    def _quarantine_spare(
        self,
        request: ModelStage2Request,
        group: ModelDuplicateGroup,
        result: ModelStage2GroupResult,
        keep: ModelDuplicateCopy,
    ) -> ModelStage2GroupResult:
        outcomes: list[ModelStage2CopyOutcome] = []
        for copy in group.copies:
            if copy is keep:
                outcomes.append(
                    ModelStage2CopyOutcome(repository=copy.repository, kept=True)
                )
                continue
            done = self._quarantine.handle(
                ModelQuarantineRequest(
                    run_id=self._next_run_id(request),
                    mode=EnumQuarantineMode.QUARANTINE,
                    repository=copy.repository,
                    entity=group.contract_name,
                    entity_paths=copy.entity_paths,
                    test_paths=copy.test_paths,
                    evidence=copy.evidence,
                )
            )
            outcomes.append(
                ModelStage2CopyOutcome(
                    repository=copy.repository,
                    kept=False,
                    outcome=done.outcome,
                    detail=done.detail,
                )
            )
        return result.model_copy(update={"copies": tuple(outcomes)})

    def _remove(
        self,
        request: ModelStage2Request,
        results: list[ModelStage2GroupResult],
        removals: dict[str, list[tuple[ModelDuplicateGroup, str]]],
    ) -> list[ModelStage2GroupResult]:
        tags = {t.repository: t.tag for t in request.tags}
        by_name = {r.contract_name: r for r in results}
        for repository, members in removals.items():
            tag = tags.get(repository, "")
            sha = self._repo.tag_sha(repository, tag) if tag else ""
            rows = tuple(
                ModelCatalogRow(
                    entity=group.contract_name,
                    tag=tag,
                    sha=sha,
                    reason=f"stage 2 duplicate of {group.contract_name}; dev loads the copy in {loaded}",
                )
                for group, loaded in members
            )
            done = self._quarantine.handle(
                ModelQuarantineRequest(
                    run_id=self._next_run_id(request),
                    mode=EnumQuarantineMode.REMOVE,
                    repository=repository,
                    batch=ModelRemovalBatch(
                        tag=tag,
                        entities=tuple(group.contract_name for group, _ in members),
                        catalog_rows=rows,
                    ),
                )
            )
            for group, _ in members:
                current = by_name[group.contract_name]
                by_name[group.contract_name] = current.model_copy(
                    update={
                        "copies": tuple(
                            c
                            if c.repository != repository
                            else ModelStage2CopyOutcome(
                                repository=repository,
                                kept=False,
                                outcome=done.outcome,
                                detail=done.detail,
                            )
                            for c in current.copies
                        )
                    }
                )
        return [by_name[r.contract_name] for r in results]
