# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run leg's suite reader: each candidate's base commit and material over git (OMN-23627).

Exemplar: ``nodes/node_code_contract_score_effect/handlers/adapter_git_workspace.py`` (git over
the registry's clone, the inherited location variables scrubbed) and
``nodes/node_code_contract_score_effect/handlers/handler_code_contract_score.py`` (a contract's
bound checks out of its dod_evidence). One material per candidate, in the candidate's order;
a task that is merely not admitted carries its ``excluded_reason`` and never raises.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml
from omnibase_core.enums.ticket.enum_dod_evidence_execution_scope import (
    EnumDodEvidenceExecutionScope,
)
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.code_eval_trial.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
    task_key_for,
)
from omnimarket.models.delegation.model_size_band import (
    ModelSizeBand,
    ModelSizeBandRequest,
)
from omnimarket.models.model_code_score import (
    ModelCodeScoreCheckFile,
    ModelCodeScoreContractRef,
    ModelCodeScoreOverlay,
)
from omnimarket.nodes.node_code_contract_score_effect.handlers.adapter_git_workspace import (
    _git,
)
from omnimarket.nodes.node_delegation_size_band_compute.handlers.handler_delegation_size_band import (
    HandlerDelegationSizeBand,
)

# the delegated code edit loop request's task limit (ModelLabCodeDraft.task max_length)
LOOP_REQUEST_TASK_MAX_CHARS = 20000


@dataclass
class _MaterialState:
    """The material's fields as the reads fill them; the neutral value when a read excludes first."""

    base_commit: str = "0" * 40
    contract_sha256: str = "0" * 64
    task_text: str = ""
    writable_globs: tuple[str, ...] = ()
    check_files: tuple[ModelCodeScoreCheckFile, ...] = ()
    held_diff: str = ""
    band: str = "S"


class GitCodeEvalSuiteReader:
    """ProtocolCodeEvalSuiteReader over git: the run leg's reads (plan rules 1, 3, 4 and 8)."""

    def __init__(
        self,
        *,
        score_overlay: ModelCodeScoreOverlay,
        size_band: HandlerDelegationSizeBand | None = None,
        max_task_chars: int,
    ) -> None:
        self._score_overlay = score_overlay
        self._size_band = (
            size_band if size_band is not None else HandlerDelegationSizeBand()
        )
        self._max_task_chars = max_task_chars

    @classmethod
    def from_overlays(
        cls, environ: Mapping[str, str] | None = None
    ) -> GitCodeEvalSuiteReader:
        """The production reader: the score overlay from the overlay roots, the loop's task limit."""
        overlay = load_structured_overlay(
            "node_code_contract_score_effect", ModelCodeScoreOverlay, environ
        )
        return cls(
            score_overlay=overlay,
            max_task_chars=LOOP_REQUEST_TASK_MAX_CHARS,
        )

    def read(
        self, candidates: Sequence[ModelCodeEvalCandidate]
    ) -> tuple[ModelCodeEvalTaskMaterial, ...]:
        """One material per candidate, in order; an exclusion is a reason, never an exception."""
        return tuple(self._read_one(candidate) for candidate in candidates)

    def _read_one(self, candidate: ModelCodeEvalCandidate) -> ModelCodeEvalTaskMaterial:
        contract_path = f"contracts/{candidate.contract_id}.yaml"
        state = _MaterialState()

        root = self._score_overlay.repo_roots.get(candidate.repository)
        if root is None:
            return self._material(
                candidate, contract_path, state, "repository_not_served"
            )

        contract_text = self._show_blob(root, candidate.merge_commit, contract_path)
        if contract_text is None:
            return self._material(candidate, contract_path, state, "contract_missing")
        state.contract_sha256 = hashlib.sha256(contract_text).hexdigest()

        try:
            contract = ModelTicketContract.model_validate(yaml.safe_load(contract_text))
        except (yaml.YAMLError, ValidationError, TypeError):
            return self._material(
                candidate, contract_path, state, "contract_unreadable"
            )

        bound = self._bound_checks(contract)
        if not bound:
            return self._material(candidate, contract_path, state, "check_not_runnable")
        for _item_id, check_type, _check_value in bound:
            if check_type not in self._score_overlay.runnable_check_types:
                return self._material(
                    candidate, contract_path, state, f"check_not_runnable:{check_type}"
                )

        base_commit = self._base_commit(root, candidate.merge_commit)
        if base_commit is None:
            return self._material(candidate, contract_path, state, "no_base_commit")
        state.base_commit = base_commit

        changed = self._changed_paths(root, base_commit, candidate.merge_commit)
        if changed is None:
            return self._material(candidate, contract_path, state, "no_base_commit")

        for _item_id, _check_type, check_value in bound:
            if not self._names_a_changed_path(check_value, changed):
                return self._material(
                    candidate, contract_path, state, "check_names_no_changed_path"
                )

        check_files = self._check_files(root, candidate.merge_commit, bound, changed)
        state.check_files = check_files

        check_paths = {file.path for file in check_files}
        held = tuple(
            path
            for path in changed
            if path != contract_path and path not in check_paths
        )
        protected = self._score_overlay.protected_paths.get(candidate.repository, ())
        for path in held:
            if self._is_protected(path, protected):
                return self._material(candidate, contract_path, state, "protected_path")
        if not held:
            return self._material(candidate, contract_path, state, "no_held_change")

        state.writable_globs = held
        state.held_diff = self._held_diff(
            root, base_commit, candidate.merge_commit, held
        )

        descriptions = tuple(item.description for item in contract.dod_evidence)
        state.task_text = "\n".join(
            [contract.title, "", contract.description, *descriptions]
        )
        if len(state.task_text) > self._max_task_chars:
            return self._material(candidate, contract_path, state, "task_too_long")

        band = self._band(contract, descriptions)
        if band is None:
            return self._material(candidate, contract_path, state, "band_unavailable")
        state.band = band

        return self._material(candidate, contract_path, state, None)

    def _material(
        self,
        candidate: ModelCodeEvalCandidate,
        contract_path: str,
        state: _MaterialState,
        excluded_reason: str | None,
    ) -> ModelCodeEvalTaskMaterial:
        if excluded_reason is None:
            material_hash = hashlib.sha256(
                "\n".join(
                    [
                        state.task_text,
                        *(f"{file.path}:{file.sha256}" for file in state.check_files),
                        state.held_diff,
                    ]
                ).encode("utf-8")
            ).hexdigest()
        else:
            material_hash = hashlib.sha256(excluded_reason.encode("utf-8")).hexdigest()
        return ModelCodeEvalTaskMaterial(
            task_key=task_key_for(candidate),
            repository=candidate.repository,
            base_commit=state.base_commit,
            task_text=state.task_text,
            writable_globs=state.writable_globs,
            contract=ModelCodeScoreContractRef(
                repository=candidate.repository,
                path=contract_path,
                revision=candidate.merge_commit,
                sha256=state.contract_sha256,
            ),
            check_files=state.check_files,
            held_diff=state.held_diff,
            band=state.band,
            material_hash=material_hash,
            excluded_reason=excluded_reason,
        )

    def _show_blob(self, root: str, revision: str, path: str) -> bytes | None:
        done = _git(root, "show", f"{revision}:{path}")
        return done.stdout if done.returncode == 0 else None

    def _base_commit(self, root: str, merge_commit: str) -> str | None:
        """The merge's first parent; None when the merge has none."""
        done = _git(root, "rev-parse", f"{merge_commit}^1")
        if done.returncode != 0:
            return None
        return done.stdout.decode(errors="replace").strip()

    def _changed_paths(
        self, root: str, base_commit: str, merge_commit: str
    ) -> tuple[str, ...] | None:
        done = _git(root, "diff", "--name-only", base_commit, merge_commit)
        if done.returncode != 0:
            return None
        return tuple(
            line for line in done.stdout.decode(errors="replace").splitlines() if line
        )

    @staticmethod
    def _bound_checks(contract: ModelTicketContract) -> list[tuple[str, str, str]]:
        """(item id, check type, check value) of every non-LOCAL_DONE_GATE item's str-valued check."""
        bound: list[tuple[str, str, str]] = []
        for item in contract.dod_evidence:
            if item.execution_scope is EnumDodEvidenceExecutionScope.LOCAL_DONE_GATE:
                continue
            for check in item.checks:
                if isinstance(check.check_value, str):
                    bound.append((item.id, check.check_type.value, check.check_value))
        return bound

    @staticmethod
    def _names_a_changed_path(check_value: str, changed: tuple[str, ...]) -> bool:
        """True when a token of the check value is a changed path or a directory prefix of one."""
        for token in check_value.split():
            if token in changed:
                return True
            prefix = token.rstrip("/") + "/"
            if any(path.startswith(prefix) for path in changed):
                return True
        return False

    def _check_files(
        self,
        root: str,
        merge_commit: str,
        bound: list[tuple[str, str, str]],
        changed: tuple[str, ...],
    ) -> tuple[ModelCodeScoreCheckFile, ...]:
        named: set[str] = set()
        for _item_id, _check_type, check_value in bound:
            for token in check_value.split():
                if token in changed or any(
                    path.startswith(token.rstrip("/") + "/") for path in changed
                ):
                    named.add(token)
        files: list[ModelCodeScoreCheckFile] = []
        for path in sorted(changed):
            if not any(
                path == named_path or path.startswith(named_path.rstrip("/") + "/")
                for named_path in named
            ):
                continue
            data = self._show_blob(root, merge_commit, path)
            if data is None:  # deleted by the change: not a check file
                continue
            files.append(
                ModelCodeScoreCheckFile(
                    path=path,
                    sha256=hashlib.sha256(data).hexdigest(),
                    revision=merge_commit,
                )
            )
        return tuple(files)

    @staticmethod
    def _is_protected(path: str, protected: tuple[str, ...]) -> bool:
        for prefix in protected:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return True
        return False

    def _held_diff(
        self, root: str, base_commit: str, merge_commit: str, held: tuple[str, ...]
    ) -> str:
        """The merged change's diff over the held paths only: the control's draft."""
        done = _git(root, "diff", base_commit, merge_commit, "--", *held)
        return done.stdout.decode(errors="replace") if done.returncode == 0 else ""

    def _band(
        self, contract: ModelTicketContract, descriptions: tuple[str, ...]
    ) -> str | None:
        """The size band of the delegated loop's request; None when the compute refuses it."""
        result = self._size_band.handle(
            ModelSizeBandRequest(
                task_class="code_generation",
                prompt=f"{contract.title}\n\n{contract.description}",
                acceptance_criteria=descriptions,
            )
        )
        return result.band.value if isinstance(result, ModelSizeBand) else None


__all__ = ["LOOP_REQUEST_TASK_MAX_CHARS", "GitCodeEvalSuiteReader"]
