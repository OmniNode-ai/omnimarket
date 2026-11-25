# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-proof gate of landing: one pure decision over three injected readers.

A pull request merges only when the lab proof it carries is for the change that merges, and never under an open
do-not-land. The answer is PROVEN or DRAFT; a DRAFT names every reason. It fails closed: a proof, a patch id or a
ledger that cannot be read answers DRAFT with the reason "proof unreadable".

* A Lab line naming a commit is required; a commit that is not the head must have the head's patch id
  (``base...sha``), so only a base merge may differ.
* A change to a grader, check, gate or judge also names a known good answer that passed and a known bad one that failed.
* An open HOLD row or do-not-land message naming the pull request answers DRAFT whatever is green.
* A documentation-only or test-duration-only change needs no Lab line.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    EnumProofAnswer,
    EnumProofCode,
    ModelProofGatePolicy,
    ModelProofGateRequest,
    ModelProofGateResult,
    ModelProofReason,
    ModelProofText,
)
from omnimarket.nodes.node_landing_proof_gate_compute.protocols.protocol_proof_gate import (
    ProtocolLedgerReader,
    ProtocolPatchIdReader,
    ProtocolProofReader,
)

UNREAD = "proof unreadable"


class HandlerLandingProofGate:
    def __init__(
        self,
        policy: ModelProofGatePolicy,
        proof: ProtocolProofReader,
        ledger: ProtocolLedgerReader,
        patch_ids: ProtocolPatchIdReader,
    ) -> None:
        self._policy = policy
        self._proof = proof
        self._ledger = ledger
        self._patch_ids = patch_ids

    def handle(self, request: ModelProofGateRequest) -> ModelProofGateResult:
        reasons: list[ModelProofReason] = []
        if request.check_ledger:
            notices = self._ledger.open_notices(request.repo, request.number)
            if notices is None:
                reasons.append(
                    ModelProofReason(
                        code=EnumProofCode.PROOF_UNREADABLE,
                        item="ledger",
                        detail=UNREAD,
                    )
                )
            else:
                reasons += [
                    ModelProofReason(
                        code=EnumProofCode.DO_NOT_LAND,
                        item=n.row_id,
                        detail=f"{n.rtype} {n.ts} names the pull request and no RELEASE lifted it",
                    )
                    for n in notices
                ]
        text = self._proof.read(
            request.repo, request.number, request.base, request.head
        )
        if text is None or not text.files:
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.PROOF_UNREADABLE,
                    item="pull request text and changed files",
                    detail=UNREAD,
                )
            )
            return self._result(request, reasons)
        exempt = self._exempt(text.files)
        if exempt:
            return self._result(request, reasons, exempt=exempt)
        proof = self._lab_proof(request, text, reasons)
        self._known_answers(text, reasons)
        return self._result(request, reasons, proof=proof)

    @staticmethod
    def _result(
        request: ModelProofGateRequest,
        reasons: list[ModelProofReason],
        exempt: str = "",
        proof: str = "",
    ) -> ModelProofGateResult:
        return ModelProofGateResult(
            answer=EnumProofAnswer.DRAFT if reasons else EnumProofAnswer.PROVEN,
            reasons=tuple(reasons),
            exempt=exempt,
            proof=proof,
            ledger_checked=request.check_ledger,
        )

    def _is_test(self, path: str) -> bool:
        return any(m in path for m in self._policy.test_path_markers)

    def _exempt(self, files: tuple[str, ...]) -> str:
        """Why every changed file needs no Lab line, or "". A ``.py`` file is never exempt."""
        p = self._policy
        kinds: set[str] = set()
        for f in files:
            if f.endswith(".py"):
                return ""
            if any(m in f for m in p.duration_path_markers):
                kinds.add("test_durations")
            elif any(m in f for m in p.doc_path_markers) or f.endswith(
                p.doc_extensions
            ):
                kinds.add("docs")
            else:
                return ""
        return "+".join(sorted(kinds))

    def _all_lines(self, text: ModelProofText) -> list[str]:
        return "\n".join([text.body, *text.comments]).splitlines()

    def _lab_proof(
        self,
        request: ModelProofGateRequest,
        text: ModelProofText,
        reasons: list[ModelProofReason],
    ) -> str:
        lab = re.compile(self._policy.lab_line)
        sha = re.compile(self._policy.sha)
        failed = re.compile(self._policy.lab_failed)
        lab_lines = [
            ln for ln in self._all_lines(text) if lab.match(ln) and sha.search(ln)
        ]
        shas = sorted(
            {x for ln in lab_lines if not failed.search(ln) for x in sha.findall(ln)}
        )
        if lab_lines and not shas:
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.LAB_RUN_FAILED,
                    item="a Lab line recording a run that passed",
                    detail="every Lab line records a refused or failed run",
                )
            )
            return ""
        if not shas:
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.LAB_LINE_MISSING,
                    item="a Lab line naming a commit",
                )
            )
            return ""
        if any(request.head.startswith(x) for x in shas):
            return "head"
        want = self._patch_ids.patch_id(request.repo, request.base, request.head)
        if want is None:
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.PROOF_UNREADABLE,
                    item=f"patch id of {request.base}...{request.head[:9]}",
                    detail=UNREAD,
                )
            )
            return ""
        if any(
            self._patch_ids.patch_id(request.repo, request.base, x) == want
            for x in shas
        ):
            return "patch_id"
        reasons.append(
            ModelProofReason(
                code=EnumProofCode.LAB_PROOF_OTHER_CHANGE,
                item=f"a Lab commit that is the head {request.head[:9]} or has its patch id",
                detail=f"Lab names {', '.join(x[:9] for x in shas)}",
            )
        )
        return ""

    def _known_answers(
        self, text: ModelProofText, reasons: list[ModelProofReason]
    ) -> None:
        touched = [
            f
            for f in text.files
            if not self._is_test(f)
            and any(w in f.lower() for w in self._policy.gated_words)
        ]
        if not touched:
            return
        lines = self._all_lines(text)
        if not any(re.search(self._policy.known_good, ln) for ln in lines):
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.KNOWN_GOOD_MISSING,
                    item="a known good answer that passed",
                    detail=f"touches {touched[0]}",
                )
            )
        if not any(re.search(self._policy.known_bad, ln) for ln in lines):
            reasons.append(
                ModelProofReason(
                    code=EnumProofCode.KNOWN_BAD_MISSING,
                    item="a known bad answer that failed",
                    detail=f"touches {touched[0]}",
                )
            )
