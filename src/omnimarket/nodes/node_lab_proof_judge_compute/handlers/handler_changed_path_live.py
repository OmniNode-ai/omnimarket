# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerChangedPathLive: the changed-path-live check as a pure function.

Definition-B compute: ``handle(request: ModelChangedPathLiveRequest) -> ModelChangedPathLiveResult``.
No I/O, no clock, no hard-coded values: every setting comes from the overlay.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_live_probe import (
    HandlerLiveProbe,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_judge_verdict import (
    EnumJudgeVerdict,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_use_outcome import (
    EnumUseOutcome,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
    ModelChangedPathLiveRequest,
    ModelChangedPathLiveResult,
    ModelFoundProof,
    ModelMatchedEntry,
    ModelNotApplicableUse,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_live_probe import (
    ModelLiveProbeRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)


class HandlerChangedPathLive:
    """The changed-path-live check: pure, no I/O, no clock."""

    def __init__(self, overlay: ModelLabProofJudgeOverlay) -> None:
        self._overlay = overlay

    def qualify(self, repo: str) -> str:
        """The declared repository a bare name stands for (the clone phase prints ``omnimarket``), else ``repo`` as given."""
        named = [
            r.repo
            for r in self._overlay.not_applicable.repos
            if r.repo.rsplit("/", 1)[-1] == repo
        ]
        return named[0] if len(named) == 1 else repo

    def handle(
        self, request: ModelChangedPathLiveRequest
    ) -> ModelChangedPathLiveResult:
        """Compute the changed-path-live verdict for the request.

        A request that carries the lab probe's text has the base verdict and the contract-local nodes read from it.
        """
        if request.probe is None:
            return self._judge(
                request,
                bool(request.base_changed_path_live),
                request.contract_local_nodes,
            )
        probed = HandlerLiveProbe().handle(ModelLiveProbeRequest(probe=request.probe))
        if probed.base is None:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.NO_CHECK,
                ok=False,
                evidence="the probe printed no live-grep line, so there is no changed-path check",
            )
        result = self._judge(request, probed.base, probed.contract_local_nodes)
        evidence = "; ".join([result.evidence, *probed.notes])
        return result.model_copy(update={"evidence": evidence, "notes": probed.notes})

    def _judge(
        self,
        request: ModelChangedPathLiveRequest,
        base_live: bool,
        contract_local_nodes: tuple[str, ...],
    ) -> ModelChangedPathLiveResult:
        # 1. If the base judge already passed, nothing changes.
        if base_live:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.PASS,
                ok=True,
                class_id=None,
                evidence="judge read the changed path live",
            )

        # 2. Look up the repo's not-applicable declaration.
        repo_decl = None
        for r in self._overlay.not_applicable.repos:
            if r.repo == request.repo:
                repo_decl = r
                break
        if repo_decl is None:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.FAIL,
                ok=False,
                class_id=None,
                evidence=f"no not-applicable declaration for {request.repo}",
            )

        # 3. Find the changed runtime nodes.
        node_pattern = re.compile(repo_decl.node_path_regex)
        non_runtime_patterns = [re.compile(rx) for rx in repo_decl.non_runtime_regex]
        changed_nodes: set[str] = set()
        for path in request.changed_paths:
            if any(rx.search(path) for rx in non_runtime_patterns):
                continue
            m = node_pattern.search(path)
            if m and "node" in m.groupdict():
                changed_nodes.add(m.group("node"))
        changed_nodes -= set(contract_local_nodes)
        if not changed_nodes and all(
            any(rx.search(path) for rx in non_runtime_patterns)
            for path in request.changed_paths
        ):
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.PASS,
                ok=True,
                class_id=None,
                evidence="every changed path is a declared non-runtime path (tests only); no runtime behaviour changed",
            )
        if not changed_nodes:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.FAIL,
                ok=False,
                class_id=None,
                evidence="no runtime node was changed",
            )

        # 4. Check that every changed node is declared.
        declared = {e.node: e for e in repo_decl.entries}
        unmatched = sorted(changed_nodes - set(declared))
        if unmatched:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.FAIL,
                ok=False,
                class_id=None,
                unmatched_nodes=tuple(unmatched),
                evidence=f"change touches runtime node(s) the declaration does not name: {', '.join(unmatched)}",
            )

        # 5. Every changed node is declared. Find substitute receipts.
        matched_entries: list[ModelMatchedEntry] = []
        substitute_proofs: list[ModelFoundProof] = []
        missing: list[str] = []
        receipt_ids: list[str] = []
        for node in sorted(changed_nodes):
            entry = declared[node]
            matched_entries.append(
                ModelMatchedEntry(
                    node=node,
                    runs_by=entry.runs_by,
                    reason=entry.reason,
                    substitute_how=entry.substitute_proof.how,
                )
            )
            found = None
            for rcpt in request.substitute_receipts:
                if (
                    rcpt.repo == request.repo
                    and rcpt.node == node
                    and rcpt.head == request.head
                    and rcpt.exit_code == 0
                    and rcpt.host_class in entry.substitute_proof.host_classes
                ):
                    found = rcpt
                    break
            if found is not None:
                substitute_proofs.append(
                    ModelFoundProof(
                        node=node,
                        receipt_id=found.receipt_id,
                        host=found.host,
                        host_class=found.host_class,
                        command=found.command,
                        finished_at=found.finished_at.isoformat(),
                        log_path=found.log_path,
                    )
                )
                receipt_ids.append(found.receipt_id)
            else:
                missing.append(node)

        class_id = self._overlay.not_applicable.class_id
        nodes_tuple = tuple(sorted(changed_nodes))

        if missing:
            return ModelChangedPathLiveResult(
                verdict=EnumJudgeVerdict.FAIL,
                ok=False,
                class_id=class_id,
                matched_entries=tuple(matched_entries),
                substitute_proofs=tuple(substitute_proofs),
                missing_proof_nodes=tuple(missing),
                evidence=f"missing substitute proof for: {', '.join(missing)}",
                use=ModelNotApplicableUse(
                    repo=request.repo,
                    pr_number=request.pr_number,
                    head=request.head,
                    class_id=class_id,
                    outcome=EnumUseOutcome.REFUSED_NO_SUBSTITUTE,
                    nodes=nodes_tuple,
                    receipt_ids=tuple(receipt_ids),
                ),
            )

        return ModelChangedPathLiveResult(
            verdict=EnumJudgeVerdict.NOT_APPLICABLE,
            ok=True,
            class_id=class_id,
            matched_entries=tuple(matched_entries),
            substitute_proofs=tuple(substitute_proofs),
            evidence=(
                f"NOT-APPLICABLE class={class_id} entries={','.join(nodes_tuple)} "
                f"substitute={','.join(receipt_ids)}"
            ),
            use=ModelNotApplicableUse(
                repo=request.repo,
                pr_number=request.pr_number,
                head=request.head,
                class_id=class_id,
                outcome=EnumUseOutcome.NOT_APPLICABLE,
                nodes=nodes_tuple,
                receipt_ids=tuple(receipt_ids),
            ),
        )
