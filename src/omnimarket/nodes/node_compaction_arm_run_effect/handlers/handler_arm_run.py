# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fork one frozen prefix per arm, compact it, and record the context, usage and hashes (CM.10).

The compactor is the only variable between the arms. Every arm of a case gets its own copy of
the prefix and the ledger and its own configuration directory under the run root; the copies are
hashed against the source before any compactor runs and again after it, so an arm that sees a
different input, or that rewrites the prefix, removes the case from every arm. Nothing outside
the replay root is read.

The two headless premises are falsified first. When either is not established, ``stock_rerun``
is ``REFUSED`` and the result says so; ``stock_recorded`` is then the stock arm (label
``RECORDED_STOCK``). A ``stock_rerun`` fork that shows any custom-boundary evidence is
``INVALID`` and is never scorable. The live adapters (a headless Claude Code session and the
pinned pack build) are injected; without them the arms are refused with a typed reason.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.models.context_install_confirm import EnumBoundaryKind
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
    ProtocolArmAdapter,
    ProtocolHeadlessPremises,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    EnumArm,
    EnumArmKind,
    EnumArmStatus,
    EnumArmTrack,
    EnumPremise,
    EnumReplySource,
    EnumStockBranch,
    ModelAdapterOutput,
    ModelArmAdapterSpec,
    ModelArmCase,
    ModelArmRecord,
    ModelArmRunRequest,
    ModelArmRunResult,
    ModelFork,
    ModelPremiseReport,
    ModelRemovedCase,
)

__all__ = ["HandlerCompactionArmRun", "LiveReadRefusedError", "load_arm_roster"]

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_PREFIX_DIR = "prefix"
_LEDGER_DIR = "ledger"
_CONFIG_DIR = "config"


class LiveReadRefusedError(ValueError):
    """A case file lies outside the replay root: a live read, which fails and never warns."""


def load_arm_roster() -> tuple[ModelArmAdapterSpec, ...]:
    """The arm adapters, as the contract declares them."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return tuple(
        ModelArmAdapterSpec.model_validate(row) for row in contract["arm_adapters"]
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


class _FaultError(Exception):
    """An infrastructure fault that removes a case from every arm."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ForkHashes:
    def __init__(self, prefix: str, ledger: str) -> None:
        self.prefix = prefix
        self.ledger = ledger
        self.state = _sha256_text(f"prefix:{prefix}\nledger:{ledger}")
        self.compaction_input = _sha256_text(f"compaction-input:{prefix}:{ledger}")

    def key(self) -> tuple[str, str, str, str]:
        return (self.prefix, self.ledger, self.state, self.compaction_input)


class _Fork:
    def __init__(self, view: ModelFork, hashes: _ForkHashes) -> None:
        self.view = view
        self.hashes = hashes

    def rehash(self) -> _ForkHashes:
        return _ForkHashes(
            _sha256_file(Path(self.view.prefix_path)),
            _sha256_file(Path(self.view.ledger_path)),
        )


class _Draft:
    """An arm's result held in memory until its case is known to be clean."""

    def __init__(
        self,
        fork: _Fork,
        status: EnumArmStatus,
        reason: str | None = None,
        output: ModelAdapterOutput | None = None,
        context: str | None = None,
    ) -> None:
        self.fork = fork
        self.status = status
        self.reason = reason
        self.output = output
        self.context = context


def _track_of(
    roster: Mapping[EnumArm, ModelArmAdapterSpec], arm: EnumArm
) -> EnumArmTrack:
    return roster[arm].track


class HandlerCompactionArmRun:
    def __init__(
        self,
        premises: ProtocolHeadlessPremises | None = None,
        adapters: Mapping[EnumArm, ProtocolArmAdapter] | None = None,
    ) -> None:
        self._premises = premises
        self._adapters = dict(adapters or {})
        self._roster = {spec.arm: spec for spec in load_arm_roster()}

    def handle(self, request: ModelArmRunRequest) -> ModelArmRunResult:
        replay_root = Path(request.replay_root)
        run_root = Path(request.run_root)
        for case in request.cases:
            for raw in (case.prefix_path, case.ledger_path):
                if not _inside(Path(raw), replay_root):
                    raise LiveReadRefusedError(
                        f"{case.case_id}: {raw} is outside the replay root"
                    )
        premises = self._falsify_premises(request, run_root)
        stock_reason = self._stock_refusal(request, premises)
        refused = stock_reason is not None

        records: list[ModelArmRecord] = []
        removed: list[ModelRemovedCase] = []
        for case in request.cases:
            try:
                drafts = self._run_case(request, case, run_root, stock_reason)
            except _FaultError as fault:
                removed.append(
                    ModelRemovedCase(case_id=case.case_id, reason=fault.reason)
                )
                continue
            for arm, draft in drafts:
                records.append(self._write_record(request, run_root, arm, case, draft))

        mod_recorded = [
            r
            for r in records
            if r.arm is EnumArm.MOD and r.status is EnumArmStatus.RECORDED
        ]
        fallbacks = [
            r for r in mod_recorded if r.reply_source is EnumReplySource.NATIVE
        ]
        return ModelArmRunResult(
            correlation_id=request.correlation_id,
            premises=premises,
            stock_branch=(
                EnumStockBranch.RECORDED_STOCK if refused else EnumStockBranch.RERUN
            ),
            stock_rerun_refused=refused,
            refusal_statement=(
                None
                if stock_reason is None
                else (
                    f"stock_rerun REFUSED: {stock_reason}. stock_recorded is the stock arm "
                    "(RECORDED_STOCK); the planted stratum has no stock arm, so its "
                    "safeguard is NOT_ESTABLISHED."
                )
            ),
            report_label="RECORDED_STOCK" if refused else None,
            planted_stratum_safeguard="NOT_ESTABLISHED" if refused else None,
            records=tuple(records),
            removed_cases=tuple(removed),
            mod_fallback_count=len(fallbacks),
            mod_recorded_count=len(mod_recorded),
            mod_fallback_rate=(
                len(fallbacks) / len(mod_recorded) if mod_recorded else None
            ),
        )

    # ------------------------------------------------------------------ premises

    def _falsify_premises(
        self, request: ModelArmRunRequest, run_root: Path
    ) -> ModelPremiseReport:
        if EnumArm.STOCK_RERUN not in request.arms or self._premises is None:
            return ModelPremiseReport(
                manual_compaction=EnumPremise.UNVERIFIED,
                fork_keeps_transcript=EnumPremise.UNVERIFIED,
            )
        probe_case = request.cases[0]
        probe = self._fork(
            run_root / "premise", probe_case, EnumArm.STOCK_RERUN, request.repeat_index
        )
        return ModelPremiseReport(
            manual_compaction=_premise(
                self._premises.manual_compaction_accepted(probe.view)
            ),
            fork_keeps_transcript=_premise(
                self._premises.fork_keeps_transcript(probe.view)
            ),
        )

    def _stock_refusal(
        self, request: ModelArmRunRequest, premises: ModelPremiseReport
    ) -> str | None:
        if EnumArm.STOCK_RERUN not in request.arms:
            return None
        if not premises.passed:
            return (
                "PREMISE_NOT_ESTABLISHED: "
                f"manual_compaction={premises.manual_compaction.value}, "
                f"fork_keeps_transcript={premises.fork_keeps_transcript.value}"
            )
        if EnumArm.STOCK_RERUN not in self._adapters:
            return "STOCK_ADAPTER_UNAVAILABLE"
        return None

    # ------------------------------------------------------------------ one case

    def _fork(self, base: Path, case: ModelArmCase, arm: EnumArm, repeat: int) -> _Fork:
        """A fresh copy of the case in the arm's own directories; a rerun restores the snapshot."""
        root = base / case.case_id / arm.value / f"r{repeat}"
        if root.exists():
            shutil.rmtree(root)
        prefix = root / _PREFIX_DIR / Path(case.prefix_path).name
        ledger = root / _LEDGER_DIR / Path(case.ledger_path).name
        config = root / _CONFIG_DIR
        config.mkdir(parents=True)
        _copy_file(Path(case.prefix_path), prefix)
        _copy_file(Path(case.ledger_path), ledger)
        view = ModelFork(
            case_id=case.case_id,
            arm=arm,
            track=_track_of(self._roster, arm),
            root=str(root),
            prefix_path=str(prefix),
            ledger_path=str(ledger),
            config_dir=str(config),
        )
        fork = _Fork(view, _ForkHashes("", ""))
        fork.hashes = fork.rehash()
        return fork

    def _run_case(
        self,
        request: ModelArmRunRequest,
        case: ModelArmCase,
        run_root: Path,
        stock_reason: str | None,
    ) -> list[tuple[EnumArm, _Draft]]:
        source = _ForkHashes(
            _sha256_file(Path(case.prefix_path)), _sha256_file(Path(case.ledger_path))
        )
        forks = {
            arm: self._fork(run_root / "forks", case, arm, request.repeat_index)
            for arm in request.arms
        }
        if any(fork.hashes.key() != source.key() for fork in forks.values()):
            raise _FaultError("FORK_HASH_MISMATCH")
        drafts: list[tuple[EnumArm, _Draft]] = []
        for arm, fork in forks.items():
            draft = self._compact(arm, case, fork, stock_reason)
            if fork.rehash().key() != source.key():
                raise _FaultError("PREFIX_MUTATED")
            drafts.append((arm, draft))
        return drafts

    def _compact(
        self, arm: EnumArm, case: ModelArmCase, fork: _Fork, stock_reason: str | None
    ) -> _Draft:
        kind = self._roster[arm].kind
        if kind is EnumArmKind.CONTROL:
            context = {
                EnumArm.ORACLE: case.oracle_context,
                EnumArm.EMPTY: "",
                EnumArm.INVERTED: case.inverted_context,
            }[arm]
            return _Draft(fork, EnumArmStatus.RECORDED, context=context)
        if kind is EnumArmKind.RECORDED_SUMMARY:
            if case.recorded_summary is None:
                return _Draft(fork, EnumArmStatus.UNAVAILABLE, "SUMMARY_UNVERIFIED")
            return _Draft(fork, EnumArmStatus.RECORDED, context=case.recorded_summary)
        if arm is EnumArm.STOCK_RERUN and stock_reason is not None:
            return _Draft(fork, EnumArmStatus.REFUSED, stock_reason)
        adapter = self._adapters.get(arm)
        if adapter is None:
            return _Draft(fork, EnumArmStatus.REFUSED, "MOD_ADAPTER_UNAVAILABLE")
        output = adapter.compact(fork.view)
        if output is None or output.compacted_context is None:
            return _Draft(fork, EnumArmStatus.UNAVAILABLE, "NO_OUTPUT")
        if arm is EnumArm.STOCK_RERUN:
            if (
                output.boundary_kind is EnumBoundaryKind.CUSTOM
                or output.custom_boundary_evidence
            ):
                return _Draft(
                    fork,
                    EnumArmStatus.INVALID,
                    "CUSTOM_BOUNDARY_EVIDENCE",
                    output=output,
                )
            if output.boundary_kind is EnumBoundaryKind.UNREADABLE:
                return _Draft(
                    fork,
                    EnumArmStatus.UNAVAILABLE,
                    "BOUNDARY_UNREADABLE",
                    output=output,
                )
        return _Draft(
            fork,
            EnumArmStatus.RECORDED,
            output=output,
            context=output.compacted_context,
        )

    # ------------------------------------------------------------------ records

    def _write_record(
        self,
        request: ModelArmRunRequest,
        run_root: Path,
        arm: EnumArm,
        case: ModelArmCase,
        draft: _Draft,
    ) -> ModelArmRecord:
        """Append-only: a rerun adds attempt-N files and never rewrites an earlier one."""
        directory = (
            run_root / "records" / case.case_id / arm.value / f"r{request.repeat_index}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        attempt = len(list(directory.glob("attempt-*.json"))) + 1
        hashes = draft.fork.hashes
        output = draft.output
        record = ModelArmRecord(
            case_id=case.case_id,
            arm=arm,
            track=draft.fork.view.track,
            repeat_index=request.repeat_index,
            status=draft.status,
            reason=draft.reason,
            prefix_sha256=hashes.prefix,
            ledger_sha256=hashes.ledger,
            initial_state_hash=hashes.state,
            compaction_input_hash=hashes.compaction_input,
            fork_root=draft.fork.view.root,
            compacted_context_sha256=(
                _sha256_text(draft.context)
                if draft.context is not None and draft.status is EnumArmStatus.RECORDED
                else None
            ),
            input_tokens=output.input_tokens if output else None,
            output_tokens=output.output_tokens if output else None,
            reply_source=output.reply_source if output else None,
            boundary_kind=output.boundary_kind if output else None,
            attempt=attempt,
        )
        if draft.context is not None and draft.status is EnumArmStatus.RECORDED:
            with (directory / f"attempt-{attempt}.txt").open(
                "x", encoding="utf-8"
            ) as handle:
                handle.write(draft.context)
        with (directory / f"attempt-{attempt}.json").open(
            "x", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True))
        return record


def _premise(answer: bool | None) -> EnumPremise:
    if answer is None:
        return EnumPremise.UNVERIFIED
    return EnumPremise.PASS if answer else EnumPremise.FAIL
