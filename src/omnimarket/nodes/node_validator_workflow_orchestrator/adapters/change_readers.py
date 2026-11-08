# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read effect on both backends: a planted change in memory, and a git repository (OMN-23078).

Both build the same typed validator input from a change. The git reader uses omnibase_core's own
fact reader for Canonical File Shape (``gather_facts``), so the facts are the ones the validator's
own runtime reads.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.models.validation.model_canonical_file_shape_change import (
    ModelCanonicalFileShapeChange,
)
from omnibase_core.models.validation.model_canonical_file_shape_check_input import (
    ModelCanonicalFileShapeCheckInput,
)
from omnibase_core.models.validation.model_canonical_file_shape_facts import (
    ModelCanonicalFileShapeFacts,
)
from omnibase_core.models.validation.model_canonical_file_shape_text import (
    ModelCanonicalFileShapeText,
)
from omnibase_core.models.validation.model_canonical_file_shape_tree import (
    ModelCanonicalFileShapeTree,
)
from omnibase_core.validation.checker_canonical_file_shape import load_rule_set
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_validator_verdict_reducer.models.model_validator_verdict import (
    ModelRatchetReading,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    ModelChangeRead,
    ModelValidatorWorkflowRequest,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    ModelRatchetSource,
)

INPUT_CANONICAL_FILE_SHAPE = "canonical_file_shape"


class UnsupportedInputKindError(ValueError):
    """The overlay names an input kind this reader cannot build."""


def count_entries(text: str | None, comment_prefix: str) -> int:
    if text is None:
        return 0
    return sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(comment_prefix)
    )


def readings(
    sources: tuple[ModelRatchetSource, ...], head_text: dict[str, str | None]
) -> tuple[ModelRatchetReading, ...]:
    return tuple(
        ModelRatchetReading(
            key=s.key, value=count_entries(head_text.get(s.path), s.comment_prefix)
        )
        for s in sources
        if head_text.get(s.path) is not None
    )


class ModelPlantedChange(BaseModel):
    """A pull request held in memory: every file at base and at head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_files: dict[str, str] = Field(default_factory=dict)
    head_files: dict[str, str] = Field(default_factory=dict)
    commit_message: str = "planted change"

    def changed(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                p
                for p, text in self.head_files.items()
                if self.base_files.get(p) != text
            )
        )


class InMemoryChangeReader:
    """The default read effect: planted changes keyed by ``repo#pr``."""

    def __init__(
        self,
        changes: dict[str, ModelPlantedChange],
        ratchet_sources: tuple[ModelRatchetSource, ...],
    ) -> None:
        self._changes = changes
        self._sources = ratchet_sources

    def _change(self, request: ModelValidatorWorkflowRequest) -> ModelPlantedChange:
        key = f"{request.repo}#{request.pr}"
        if key not in self._changes:
            raise LookupError(f"no planted change for {key}")
        return self._changes[key]

    def read_change(self, request: ModelValidatorWorkflowRequest) -> ModelChangeRead:
        change = self._change(request)
        return ModelChangeRead(
            changed_paths=change.changed(),
            ratchet_readings=readings(self._sources, dict(change.head_files)),
        )

    def validator_input(
        self,
        request: ModelValidatorWorkflowRequest,
        input_kind: str,
        validation_contract: object,
    ) -> BaseModel:
        if input_kind != INPUT_CANONICAL_FILE_SHAPE:
            raise UnsupportedInputKindError(input_kind)
        assert isinstance(validation_contract, ModelValidatorSubcontract)
        p = load_rule_set(validation_contract).parameters
        change = self._change(request)
        head, base = change.head_files, change.base_files
        tree = ModelCanonicalFileShapeTree(
            files=tuple(sorted(head)),
            contract_texts=tuple(
                ModelCanonicalFileShapeText(path=path, text=text)
                for path, text in sorted(head.items())
                if PurePosixPath(path).name == p.contract_file_name
            ),
            pyproject_text=head.get("pyproject.toml"),
        )
        facts = ModelCanonicalFileShapeFacts(
            head_tree=tree,
            has_base=True,
            head_baseline_text=head.get(p.baseline_path),
            base_baseline_text=base.get(p.baseline_path),
            head_burndown_text=head.get(p.burndown_baseline_path),
            base_burndown_text=base.get(p.burndown_baseline_path),
            changes=tuple(
                ModelCanonicalFileShapeChange(
                    status="M" if path in base else "A",
                    path=path,
                    new_text=head[path],
                    old_text=base.get(path),
                )
                for path in change.changed()
            ),
            head_lifecycle_text=head.get(p.lifecycle_path),
            base_lifecycle_text=base.get(p.lifecycle_path),
            commit_messages=(change.commit_message,),
        )
        return ModelCanonicalFileShapeCheckInput(
            facts=facts, contract=validation_contract
        )


class GitChangeReader:
    """The real read effect: a git repository, read through omnibase_core's GitRepo."""

    def __init__(
        self,
        repo_root: Path,
        ratchet_sources: tuple[ModelRatchetSource, ...],
        cat_file_timeout_s: float,
    ) -> None:
        from omnibase_core.validation.validator_git_repo import GitRepo

        self._repo = GitRepo(root=repo_root, cat_file_timeout_s=cat_file_timeout_s)
        self._sources = ratchet_sources

    def read_change(self, request: ModelValidatorWorkflowRequest) -> ModelChangeRead:
        changed = tuple(
            sorted(
                path
                for status, path, _ in self._repo.changed_paths(
                    request.base, request.head
                )
                if status != "D"
            )
        )
        blobs = self._repo.read_blobs(request.head, [s.path for s in self._sources])
        head_text = {
            path: None if blob is None else blob.decode("utf-8", errors="replace")
            for path, blob in blobs.items()
        }
        return ModelChangeRead(
            changed_paths=changed, ratchet_readings=readings(self._sources, head_text)
        )

    def validator_input(
        self,
        request: ModelValidatorWorkflowRequest,
        input_kind: str,
        validation_contract: object,
    ) -> BaseModel:
        if input_kind != INPUT_CANONICAL_FILE_SHAPE:
            raise UnsupportedInputKindError(input_kind)
        assert isinstance(validation_contract, ModelValidatorSubcontract)
        from omnibase_core.nodes.node_canonical_file_shape_check_compute.runtime_canonical_file_shape_check import (
            gather_facts,
        )

        p = load_rule_set(validation_contract).parameters
        facts = gather_facts(self._repo, p, request.head, request.base, None)
        return ModelCanonicalFileShapeCheckInput(
            facts=facts, contract=validation_contract
        )
